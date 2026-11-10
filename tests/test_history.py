"""
Module 6 tests - performance history and trend analysis.

TC-HIS-01  Snapshots are stored and read back (no duplicate CPU minutes, old rows purged)
TC-HIS-02  Trend analysis: blocking incidents, growth per day, busiest hours, clock shift
TC-HIS-03  Demo history tells consistent stories (last point = live page)
TC-HIS-04  The collector reads the real servers and saves one snapshot each (integration)
TC-HIS-05  The history page renders: demo data, no data yet, collector stopped
TC-HIS-06  Long blocking (>= BLOCKING_DETAIL_SECONDS): the blocked query and the head blocker
           with its statement are saved once per block and shown on the page
           (the live variant holds a lock in tempdb for a few seconds: marked 'blocking',
           excluded by default; run with  python -m pytest -m blocking -v)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from monitor import demo_data
from monitor.alerts import AlertStore, evaluate_rules, health_score
from monitor.collectors import SessionsSnapshot, blocking_chains, prepare_sessions
from monitor.config import DEMO_TARGETS, get_settings
from monitor.history import (HistoryStore, Snapshot, blocking_incidents, busiest_hours,
                             candidates_frame, growth_table, long_blocking_events, quietest_hour,
                             resample, run_once, server_clock_shift, unreachable_minutes,
                             watch_blocking)

APP_FILE = Path(__file__).resolve().parent.parent / "app.py"
T0 = datetime(2026, 11, 2, 9, 0)            # a Monday


def _snapshot(at: datetime, blocked=0, wait=0.0, data_mb=100.0, connected=True, cpu_minutes=0) -> Snapshot:
    values = {"health_score": np.int64(90), "user_sessions": np.int64(7), "blocked_sessions": blocked,
              "longest_block_s": wait, "sql_memory_mb": np.float64(512.0), "databases_total": 1,
              "databases_online": 1}
    dbs = pd.DataFrame([{"database_name": "SalesDB", "state_desc": "ONLINE", "is_system": 0,
                         "data_mb": data_mb, "log_mb": 10.0, "log_used_pct": np.nan}])
    cpu = pd.DataFrame([{"event_time": pd.Timestamp(at - timedelta(minutes=m)).floor("min"),
                         "sql_cpu_pct": 20.0, "other_cpu_pct": 5.0} for m in range(cpu_minutes)])
    return Snapshot("Primary", at, connected, values if connected else {"health_score": 0},
                    dbs if connected else pd.DataFrame(), cpu)


# --- TC-HIS-01 storage ------------------------------------------------------------------------
def test_store_round_trip_with_numpy_values(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.save(_snapshot(T0, cpu_minutes=10))
    store.save(_snapshot(T0 + timedelta(minutes=5), cpu_minutes=10))     # 5 minutes overlap
    h = store.load("Primary")
    assert len(h.servers) == 2 and h.servers["connected"].all()
    assert h.servers["health_score"].tolist() == [90, 90]
    assert len(h.databases) == 2 and h.databases["log_used_pct"].isna().all()     # NaN -> NULL -> NaN
    assert len(h.cpu) == 15 and h.cpu["event_time"].is_unique                       # overlap not duplicated
    assert h.first_snapshot == T0 and h.last_snapshot == T0 + timedelta(minutes=5)
    assert store.load("Secondary").empty                                             # other servers separate


def test_purge_removes_only_old_rows(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.save(_snapshot(T0 - timedelta(days=40), cpu_minutes=2))
    store.save(_snapshot(T0, cpu_minutes=2))
    assert store.purge(retention_days=30, now=T0) == 1 + 1 + 2      # server + database + 2 CPU rows
    assert store.load("Primary").servers["collected_at"].tolist() == [pd.Timestamp(T0)]


def test_unreachable_snapshot_is_recorded(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.save(_snapshot(T0, connected=False))
    h = store.load("Primary")
    assert not h.servers["connected"].iloc[0] and h.databases.empty
    assert unreachable_minutes(h.servers, 300) == 5


# --- TC-HIS-02 analysis -----------------------------------------------------------------------
def test_blocking_incidents_group_consecutive_snapshots():
    rows = [(0, 0), (5, 2), (10, 3), (15, 0), (20, 0), (25, 1)]       # minutes, blocked
    servers = pd.DataFrame({"collected_at": [T0 + timedelta(minutes=m) for m, _ in rows],
                            "blocked_sessions": [b for _, b in rows],
                            "longest_block_s": [b * 40.0 for _, b in rows]})
    inc = blocking_incidents(servers, interval_seconds=300)
    assert len(inc) == 2
    first = inc.iloc[0]
    assert first["max_blocked"] == 3 and first["longest_wait_s"] == 120 and first["minutes"] == 10
    assert inc.iloc[1]["minutes"] == 5                       # one sighting = one interval
    assert blocking_incidents(servers.assign(blocked_sessions=0), 300).empty


def test_growth_table_rate_and_projection():
    dbs = pd.DataFrame({"collected_at": [T0 + timedelta(days=d) for d in range(5)],
                        "database_name": "SalesDB", "is_system": 0,
                        "data_mb": [1000.0 + 50 * d for d in range(5)], "log_mb": 100.0})
    g = growth_table(dbs).iloc[0]
    assert g["start_mb"] == 1100 and g["end_mb"] == 1300 and g["growth_mb"] == 200
    assert g["mb_per_day"] == pytest.approx(50.0)
    assert g["projected_mb"] == pytest.approx(1300 + 30 * 50)


def test_busiest_hours_and_quietest_slot():
    times = pd.date_range(T0, periods=7 * 24, freq="h")             # one week, hourly
    cpu = pd.DataFrame({"event_time": times, "other_cpu_pct": 1.0,
                        "sql_cpu_pct": [5.0 if (t.weekday() == 6 and t.hour == 3) else 40.0 for t in times]})
    pivot = busiest_hours(cpu)
    assert pivot.shape == (7, 24)
    assert quietest_hour(pivot) == ("Sun", 3, 5.0)
    assert quietest_hour(busiest_hours(cpu.iloc[0:0])) is None


def test_resample_hourly_mean_and_max():
    df = pd.DataFrame({"t": [T0, T0 + timedelta(minutes=30), T0 + timedelta(hours=1)], "v": [1.0, 3.0, 5.0]})
    assert resample(df, "t", ["v"], "1h")["v"].tolist() == [2.0, 5.0]
    assert resample(df, "t", ["v"], "1h", how="max")["v"].tolist() == [3.0, 5.0]
    assert resample(df, "t", ["v"], None) is df


def test_server_clock_shift_rounds_to_a_time_zone():
    local = datetime(2026, 11, 10, 14, 49, 3)
    assert server_clock_shift(local, pd.Timestamp("2026-11-10 09:19:01.237")) == timedelta(hours=5, minutes=30)
    assert server_clock_shift(local, pd.Timestamp("2026-11-10 14:48:58")) == timedelta(0)
    assert server_clock_shift(local, None) == timedelta(0)


# --- TC-HIS-03 demo history ---------------------------------------------------------------------
def test_demo_history_last_point_matches_live_pages(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    s = get_settings()
    for t in DEMO_TARGETS:
        h = demo_data.history(t.server, t.name)
        live_health = demo_data.health(t.server)
        live = evaluate_rules(live_health, demo_data.sessions(t.server), demo_data.backups(t.server),
                              demo_data.memory(t.server), s, server_name=t.name)
        assert h.servers["health_score"].iloc[-1] == health_score(live_health.connected, candidates_frame(live))[0]
        assert h.sample and h.span_days == pytest.approx(14)


def test_demo_history_stories(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    finance = demo_data.history("demo-prod-02.example.local\\FINANCE", "DEMO-PROD-02")
    log = finance.databases[finance.databases["database_name"] == "FinanceDB"].set_index("collected_at")["log_used_pct"]
    assert log.iloc[-1] == 93 and log.iloc[-17] < 30          # 4 h ago (before log backups stopped)
    prod = demo_data.history("demo-prod-01.example.local", "DEMO-PROD-01")
    assert len(blocking_incidents(prod.servers, prod.interval_seconds)) == 4      # 3 past + the live chain
    growth = growth_table(prod.databases).set_index("database_name")
    assert growth.loc["SalesDB", "mb_per_day"] == pytest.approx(85, abs=1)
    dev = demo_data.history("demo-dev-01.example.local", "DEMO-DEV-01")
    assert not dev.servers["connected"].iloc[-1] and dev.servers["connected"].iloc[0]
    assert 180 <= unreachable_minutes(dev.servers, dev.interval_seconds) <= 200


# --- TC-HIS-04 collector against the real servers -----------------------------------------------
@pytest.mark.integration
def test_tc_his_04_collector_saves_one_snapshot_per_server(tmp_path, monkeypatch):
    monkeypatch.setenv("SQL_SERVERS", r"Primary=localhost;Secondary=localhost\SQLDEV2")
    s = get_settings()
    store = HistoryStore(tmp_path / "h.db")
    lines = run_once(s, store, alert_store=AlertStore(tmp_path / "a.db"))
    assert any("alerts:" in line for line in lines), lines
    for name in ("Primary", "Secondary"):
        h = store.load(name)
        assert len(h.servers) == 1 and h.servers["connected"].iloc[0], lines
        assert not h.databases.empty and "master" in set(h.databases["database_name"])
        assert len(h.cpu) > 0                                               # back-filled from the ring buffer
        assert h.cpu["event_time"].max() <= datetime.now() + timedelta(minutes=1)   # converted to local time
        assert h.cpu["event_time"].max() >= datetime.now() - timedelta(minutes=10)


def test_collector_cli_does_nothing_in_demo_mode(monkeypatch, capsys):
    monkeypatch.setenv("DEMO_MODE", "true")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "collect_history", Path(__file__).resolve().parent.parent / "tools" / "collect_history.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--once"]) == 0
    assert "nothing to collect" in capsys.readouterr().out
    assert not Path(os.environ["HISTORY_STORE_PATH"]).exists()


# --- TC-HIS-06 long blocking ------------------------------------------------------------------------
def _blocking_snapshot(wait_62_ms: int, wait_63_ms: int) -> SessionsSnapshot:
    """61 (idle, open transaction) blocks 62, which blocks 63."""
    cols = ["session_id", "login_name", "host_name", "program_name", "database_name", "session_status",
            "open_transaction_count", "has_request", "blocking_session_id", "wait_type", "wait_ms",
            "wait_resource", "cpu_ms", "elapsed_ms", "logical_reads", "sql_text", "idle_seconds"]
    rows = [
        (61, "CORP\\clerk", "PC-07", "Excel", "SalesDB", "sleeping", 1, 0, 0, None, 0, None, None, None, None,
         "BEGIN TRAN; UPDATE dbo.Orders SET Status = 'Hold' WHERE Id = 1", 600),
        (62, "APP\\svc_api", "API02", "OrderService", "SalesDB", "suspended", 1, 1, 61, "LCK_M_U", wait_62_ms,
         "KEY: 7:1 (aa)", 1, wait_62_ms, 1, "UPDATE dbo.Orders SET Status = 'Shipped' WHERE Id = 1", 0),
        (63, "APP\\svc_web", "WEB02", "OrderWebsite", "SalesDB", "suspended", 0, 1, 62, "LCK_M_S", wait_63_ms,
         "KEY: 7:1 (aa)", 1, wait_63_ms, 1, "SELECT Status FROM dbo.Orders WHERE Id = 1", 0),
    ]
    df = prepare_sessions(pd.DataFrame(rows, columns=cols), "DBMonitor-Capstone")
    return SessionsSnapshot(connected=True, collected_at_local=T0, sessions=df, chains=blocking_chains(df))


def test_long_blocking_events_only_over_the_threshold():
    events = long_blocking_events(_blocking_snapshot(400_000, 120_000), T0, threshold_seconds=300)
    assert len(events) == 1                                 # 63 has waited only 2 min
    e = events[0]
    assert e["blocked_session"] == 62 and e["blocked_by"] == 61 and e["head_blocker"] == 61
    assert e["blocked_sql"].startswith("UPDATE dbo.Orders SET Status = 'Shipped'")
    assert e["head_sql"].startswith("BEGIN TRAN") and e["head_login"] == "CORP\\clerk"
    assert e["head_status"] == "sleeping with 1 open transaction(s)" and e["chain"] == "61 → 62"
    assert e["blocked_since"] == T0 - timedelta(seconds=400)
    deep = long_blocking_events(_blocking_snapshot(400_000, 360_000), T0, 300)
    tail = next(x for x in deep if x["blocked_session"] == 63)
    assert tail["blocked_by"] == 62 and tail["head_blocker"] == 61 and tail["chain"] == "61 → 62 → 63"
    assert long_blocking_events(SessionsSnapshot(connected=False, collected_at_local=T0), T0, 300) == []


def test_one_row_per_block_however_many_checks_see_it(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    first = long_blocking_events(_blocking_snapshot(310_000, 0), T0, 300)
    later = long_blocking_events(_blocking_snapshot(372_000, 0), T0 + timedelta(seconds=61), 300)  # same wait
    assert store.record_blocking("Primary", first) == 1
    assert store.record_blocking("Primary", later) == 0                                          # merged
    again = long_blocking_events(_blocking_snapshot(305_000, 0), T0 + timedelta(minutes=30), 300)  # a new block
    assert store.record_blocking("Primary", again) == 1
    blk = store.load("Primary").blocking
    assert len(blk) == 2
    merged = blk.iloc[0]
    assert merged["longest_wait_s"] == 372 and merged["last_seen"] == pd.Timestamp(T0 + timedelta(seconds=61))
    assert merged["head_sql"].startswith("BEGIN TRAN")
    assert store.purge(retention_days=1, now=T0 + timedelta(days=3)) == 2
    assert store.load("Primary").blocking.empty


def test_snapshot_saves_its_long_blocks(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    snap = _snapshot(T0)
    snap.blocking = long_blocking_events(_blocking_snapshot(400_000, 0), T0, 300)
    store.save(snap)
    assert len(store.load("Primary").blocking) == 1


def test_demo_long_blocks(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    prod = demo_data.history("demo-prod-01.example.local", "DEMO-PROD-01").blocking
    assert set(prod["blocked_session"]) == {63, 66} and set(prod["head_blocker"]) == {58}
    assert prod.set_index("blocked_session").loc[66, "chain"] == "58 → 63 → 66"
    report = demo_data.history("demo-report-01.example.local", "DEMO-REPORT-01").blocking
    assert report.iloc[0]["head_sql"].startswith("INSERT INTO dbo.SalesHistory")
    assert demo_data.history("demo-prod-02.example.local\\FINANCE", "DEMO-PROD-02").blocking.empty  # 75 s only
    monkeypatch.setenv("BLOCKING_DETAIL_SECONDS", "600")                  # threshold is honoured
    assert set(demo_data.history("demo-prod-01.example.local", "DEMO-PROD-01").blocking["blocked_session"]) == {63}


@pytest.mark.integration
def test_watch_blocking_reads_real_servers_without_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("SQL_SERVERS", r"Primary=localhost;Secondary=localhost\SQLDEV2")
    store = HistoryStore(tmp_path / "h.db")
    assert watch_blocking(get_settings(), store) == []                   # no blocking right now
    assert store.load("Primary").blocking.empty


@pytest.mark.integration
@pytest.mark.blocking
def test_tc_his_06_live_long_blocking_is_recorded(tmp_path, monkeypatch):
    """Blocks a global temp table in tempdb for ~6 s (threshold lowered to 3 s), then rolls back."""
    import threading
    import time

    import pyodbc
    from monitor.db import build_connection_string

    monkeypatch.setenv("SQL_SERVERS", "Primary=localhost")
    monkeypatch.setenv("BLOCKING_DETAIL_SECONDS", "3")
    s = get_settings()

    def connect(app: str) -> pyodbc.Connection:
        cs = build_connection_string(s).replace(f"DATABASE={s.database}", "DATABASE=tempdb") \
                                       .replace(f"APP={s.app_name}", f"APP={app}")
        conn = pyodbc.connect(cs, timeout=5, autocommit=True)
        conn.timeout = 60
        return conn

    holder, waiter = connect("Capstone-Hist-Holder"), connect("Capstone-Hist-Waiter")
    hc = holder.cursor()
    hc.execute("IF OBJECT_ID('tempdb..##capstone_hist_block') IS NOT NULL DROP TABLE ##capstone_hist_block;"
               "CREATE TABLE ##capstone_hist_block (id int PRIMARY KEY, v int); INSERT ##capstone_hist_block VALUES (1, 0);")
    holder_spid = hc.execute("SELECT @@SPID").fetchval()
    waiter_spid = waiter.cursor().execute("SELECT @@SPID").fetchval()
    t = threading.Thread(target=lambda: waiter.cursor().execute(
        "SET LOCK_TIMEOUT 30000; SELECT v FROM ##capstone_hist_block WHERE id = 1;").fetchall(), daemon=True)
    store = HistoryStore(tmp_path / "h.db")
    try:
        hc.execute("BEGIN TRAN; UPDATE ##capstone_hist_block SET v = 1 WHERE id = 1;")
        t.start()
        time.sleep(5)
        lines = watch_blocking(s, store)
    finally:
        hc.execute("IF @@TRANCOUNT > 0 ROLLBACK;")
        t.join(timeout=30)
        hc.execute("IF OBJECT_ID('tempdb..##capstone_hist_block') IS NOT NULL DROP TABLE ##capstone_hist_block;")
        holder.close()
        waiter.close()
    blk = store.load("Primary").blocking
    assert len(blk) == 1, lines
    e = blk.iloc[0]
    assert e["blocked_session"] == waiter_spid and e["head_blocker"] == holder_spid
    # SQL Server auto-parameterises the simple SELECT: "SELECT [v] FROM [##capstone_hist_block] WHERE [id]=@1"
    assert e["blocked_sql"].startswith("SELECT") and "capstone_hist_block" in e["blocked_sql"]
    assert "UPDATE ##capstone_hist_block" in e["head_sql"]
    assert e["head_program"] == "Capstone-Hist-Holder" and str(e["wait_type"]).startswith("LCK_M_")


# --- TC-HIS-05 the page -----------------------------------------------------------------------------
def _page(server: str) -> AppTest:
    at = AppTest.from_file(str(APP_FILE), default_timeout=60)
    at.session_state["selected_server"] = server
    at.run()
    assert not at.exception, at.exception
    at.switch_page("views/history.py").run()
    assert not at.exception, at.exception
    return at


def test_history_page_demo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    at = _page("DEMO-PROD-01")
    labels = [m.label for m in at.metric]
    for expected in ("Lowest health score", "Peak SQL CPU", "Blocking incidents", "Time unreachable"):
        assert expected in labels
    growth = next(m for m in at.metric if m.label == "Fastest-growing database")
    assert growth.value == "ReportingDW" and "MB per day" in growth.delta
    assert next(m for m in at.metric if m.label == "Lowest health score").value == "80"
    text = " ".join(m.value for m in at.markdown)
    assert "Busiest hours" in text and "Quietest:" in text and "Blocking incidents" in text
    assert at.multiselect(key="hist_dbs").value[:2] == ["ReportingDW", "SalesDB"]     # biggest first
    assert "Long blocking - who blocked which query" in text
    sql = " ".join(c.value for c in at.code)                       # details of the most recent long block
    assert "SELECT InvoiceID, Status, Amount" in sql and "BEGIN TRAN" in sql
    assert any("idle with an open transaction" in c.value for c in at.caption)
    at.segmented_control(key="hist_range").set_value("24 hours").run()
    assert not at.exception, at.exception


def test_history_page_unreachable_demo_server_still_shows_history(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    at = _page("DEMO-DEV-01")
    unreachable = next(m for m in at.metric if m.label == "Time unreachable")
    assert unreachable.value.endswith("h")                     # about 3 h


@pytest.mark.integration
def test_history_page_without_history_explains_the_collector(monkeypatch):
    monkeypatch.setenv("SQL_SERVERS", r"Primary=localhost;Secondary=localhost\SQLDEV2")
    at = _page("Primary")
    assert any("No history has been recorded" in i.value for i in at.info)
    assert any("collect_history.py" in c.value for c in at.code)


@pytest.mark.integration
def test_history_page_warns_when_the_collector_stopped(monkeypatch):
    monkeypatch.setenv("SQL_SERVERS", r"Primary=localhost;Secondary=localhost\SQLDEV2")
    store = HistoryStore(Path(os.environ["HISTORY_STORE_PATH"]))
    for minutes_ago in (130, 125, 120):
        store.save(_snapshot(datetime.now().replace(microsecond=0) - timedelta(minutes=minutes_ago), cpu_minutes=5))
    at = _page("Primary")
    assert any("collector is not running" in w.value for w in at.warning)
    assert any(m.label == "Lowest health score" and m.value == "90" for m in at.metric)
