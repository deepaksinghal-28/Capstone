"""
Module 3 tests - sessions and blocking.

TC-05  Active SQL Server sessions are listed
TC-06  Blocking between test sessions is detected (needs approval: marked 'blocking',
       excluded from the default run; run with:  python -m pytest -m blocking -v)
"""

from __future__ import annotations

import threading
import time

import pandas as pd
import pyodbc
import pytest

from monitor import queries
from monitor.collectors import (blocking_chains, collect_sessions, prepare_sessions,
                                summarize_sessions)
from monitor.config import get_settings
from monitor.db import build_connection_string

APP = "DBMonitor-Capstone"


def _raw_sessions() -> pd.DataFrame:
    cols = ["session_id", "login_name", "host_name", "program_name", "database_name",
            "session_status", "open_transaction_count", "has_request", "blocking_session_id",
            "wait_type", "wait_ms", "wait_resource", "cpu_ms", "elapsed_ms", "logical_reads",
            "sql_text", "idle_seconds"]
    rows = [
        # A: idle head blocker with open transaction
        (61, "u1", "pc", "AppA", "CapstoneDemo", "sleeping", 1, 0, 0, None, 0, None, None, None, None,
         "UPDATE dbo.BlockingDemo SET Note='x' WHERE Id = 1", 40),
        # B: blocked by A and blocking C
        (62, "u2", "pc", "AppB", "CapstoneDemo", "running", 1, 1, 61, "LCK_M_S", 35_000, "KEY: 5:1",
         2, 35_000, 3, "SELECT Note FROM dbo.BlockingDemo WHERE Id = 1", 0),
        # C: blocked by B
        (63, "u3", "pc", "AppC", "CapstoneDemo", "running", 0, 1, 62, "LCK_M_S", 20_000, "KEY: 5:2",
         1, 20_000, 2, "SELECT Note FROM dbo.BlockingDemo WHERE Id = 2", 0),
        # D: plain sleeping
        (64, "u4", "pc", "SSMS", "master", "sleeping", 0, 0, 0, None, 0, None, None, None, None,
         "use [master]", 300),
        # E: latch-type negative blocking id must NOT count as blocked
        (65, "u5", "pc", "AppE", "master", "running", 0, 1, -4, "PAGELATCH_EX", 5, None,
         1, 10, 1, "SELECT 1", 0),
        # F: the dashboard itself
        (66, "me", "pc", APP, "master", "running", 0, 1, 0, None, 0, None, 1, 5, 1,
         "/* DBMonitor */ SELECT ...", 0),
    ]
    return pd.DataFrame(rows, columns=cols)


def test_prepare_sessions_flags_and_states():
    df = prepare_sessions(_raw_sessions(), APP).set_index("session_id")
    assert df.loc[61, "state"] == "head_blocker"
    assert df.loc[62, "state"] == "blocked_blocker"
    assert df.loc[63, "state"] == "blocked"
    assert df.loc[64, "state"] == "sleeping"
    assert df.loc[65, "state"] == "running"            # -4 latch id is not a blocking session
    assert bool(df.loc[66, "is_monitor"]) is True
    assert df.loc[62, "wait_seconds"] == pytest.approx(35.0)


def test_blocking_chain_back_to_head():
    chains = blocking_chains(prepare_sessions(_raw_sessions(), APP)).set_index("session_id")
    assert set(chains.index) == {62, 63}
    assert chains.loc[63, "chain"] == "61 → 62 → 63"
    assert chains.loc[63, "head_blocker"] == 61 and chains.loc[63, "depth"] == 2
    assert chains.loc[62, "chain"] == "61 → 62"


def test_blocking_chain_cycle_does_not_loop_forever():
    raw = _raw_sessions()
    raw.loc[raw["session_id"] == 61, "blocking_session_id"] = 63      # 61 → 62 → 63 → 61
    chains = blocking_chains(prepare_sessions(raw, APP))
    assert len(chains) == 3


def test_summarize_sessions_excludes_monitor():
    s = summarize_sessions(prepare_sessions(_raw_sessions(), APP))
    assert s["sessions"] == 5                          # session 66 (dashboard) excluded
    assert s["blocked"] == 2 and s["head_blockers"] == 1
    assert s["longest_wait_s"] == pytest.approx(35.0)
    assert s["open_tran"] == 2


def test_no_blocking_gives_empty_chains():
    raw = _raw_sessions()
    raw["blocking_session_id"] = 0
    assert blocking_chains(prepare_sessions(raw, APP)).empty


def test_no_user_sessions_still_has_every_column():
    """An idle instance returns zero rows; the pages must still find their columns."""
    df = prepare_sessions(_raw_sessions().iloc[0:0], APP)
    assert df.empty
    for col in ("is_monitor", "is_blocked", "is_head_blocker", "wait_seconds", "state", "state_label"):
        assert col in df.columns
    assert df[~df["is_monitor"] & df["wait_type"].notna()].empty     # the filter that crashed
    assert summarize_sessions(df)["sessions"] == 0
    assert blocking_chains(df).empty


def test_recovery_guard_removes_dm_exec_requests():
    assert "sys.dm_exec_requests" in queries.sessions_query(include_requests=True)
    assert "sys.dm_exec_requests" not in queries.sessions_query(include_requests=False)
    assert "sys.dm_os_waiting_tasks" in queries.sessions_query(include_requests=False)


# --- Integration --------------------------------------------------------------------

@pytest.mark.integration
def test_tc05_sessions_collected_from_real_server():
    snap = collect_sessions()
    assert snap.connected, snap.error
    assert snap.requests_skipped is False              # no database is recovering
    for col in ("session_id", "login_name", "state", "sql_text", "is_head_blocker"):
        assert col in snap.sessions.columns or snap.sessions.empty


def _connect(app: str) -> pyodbc.Connection:
    s = get_settings()
    cs = build_connection_string(s).replace(f"DATABASE={s.database}", "DATABASE=tempdb") \
                                   .replace(f"APP={s.app_name}", f"APP={app}")
    conn = pyodbc.connect(cs, timeout=5, autocommit=True)
    conn.timeout = 60
    return conn


@pytest.mark.integration
@pytest.mark.blocking
def test_tc06_real_blocking_is_detected():
    """Creates a short-lived global temp table in tempdb and blocks on it for a few seconds."""
    holder = _connect("Capstone-Test-Holder")
    waiter_conn = _connect("Capstone-Test-Waiter")
    hc = holder.cursor()
    hc.execute("IF OBJECT_ID('tempdb..##capstone_block_test') IS NOT NULL DROP TABLE ##capstone_block_test;"
               "CREATE TABLE ##capstone_block_test (id int PRIMARY KEY, v int);"
               "INSERT ##capstone_block_test VALUES (1, 0);")
    holder_spid = hc.execute("SELECT @@SPID").fetchval()
    waiter_spid = waiter_conn.cursor().execute("SELECT @@SPID").fetchval()
    done = {}

    def wait_for_row():
        cur = waiter_conn.cursor()
        cur.execute("SET LOCK_TIMEOUT 30000; SELECT v FROM ##capstone_block_test WHERE id = 1;")
        done["rows"] = cur.fetchall()

    try:
        hc.execute("BEGIN TRAN; UPDATE ##capstone_block_test SET v = 1 WHERE id = 1;")
        t = threading.Thread(target=wait_for_row, daemon=True)
        t.start()
        time.sleep(2)
        snap = collect_sessions()
        assert snap.connected, snap.error
        chains = snap.chains.set_index("session_id")
        assert waiter_spid in chains.index
        assert chains.loc[waiter_spid, "blocked_by"] == holder_spid
        assert chains.loc[waiter_spid, "head_blocker"] == holder_spid
        assert str(chains.loc[waiter_spid, "wait_type"]).startswith("LCK_M_")
        head = snap.sessions.set_index("session_id").loc[holder_spid]
        assert bool(head["is_head_blocker"]) and head["open_transaction_count"] >= 1
    finally:
        hc.execute("IF @@TRANCOUNT > 0 ROLLBACK;")
        t.join(timeout=30)
        hc.execute("IF OBJECT_ID('tempdb..##capstone_block_test') IS NOT NULL DROP TABLE ##capstone_block_test;")
        holder.close()
        waiter_conn.close()
    assert done.get("rows") == [(0,)] or [tuple(r) for r in done.get("rows", [])] == [(0,)]
