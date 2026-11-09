"""
Module 5 tests - rule-based alerts.

TC-09  Rule-based alerts (each rule, severities, recommendations, no duplicates)
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pandas as pd
import pytest

from monitor.alerts import (RUNBOOK, AlertCandidate, AlertStore, MemoryInfo, collect_memory,
                            evaluate_rules, rule_backups, rule_collection, rule_connection,
                            rule_databases, rule_memory, rule_sessions, summarize_alerts)
from monitor.collectors import (BackupPolicy, BackupSnapshot, HealthSnapshot, SessionsSnapshot,
                                blocking_chains, prepare_sessions)
from monitor.config import Settings, get_settings

S = Settings()                       # defaults: log 75/90 %, blocking 30 s, long-running 60 s
NOW = datetime(2026, 11, 9, 18, 0, 0)


def _health(connected=True, rows=None, error=None) -> HealthSnapshot:
    cols = ["database_name", "state_desc", "user_access_desc", "log_used_pct"]
    return HealthSnapshot(connected=connected, collected_at_local=NOW, error=error,
                          databases=pd.DataFrame(rows or [], columns=cols))


def _sessions(rows) -> SessionsSnapshot:
    cols = ["session_id", "login_name", "host_name", "program_name", "database_name",
            "session_status", "open_transaction_count", "has_request", "blocking_session_id",
            "wait_type", "wait_ms", "wait_resource", "cpu_ms", "elapsed_ms", "logical_reads",
            "sql_text", "idle_seconds"]
    df = prepare_sessions(pd.DataFrame(rows, columns=cols), S.app_name)
    return SessionsSnapshot(connected=True, collected_at_local=NOW, sessions=df,
                            chains=blocking_chains(df))


# --- Individual rules -------------------------------------------------------------

def test_connection_failure_is_critical_with_runbook():
    alerts = rule_connection(_health(False, error="Cannot reach SQL Server 'localhost'."), S)
    assert len(alerts) == 1
    a = alerts[0]
    assert (a.rule, a.severity, a.target) == ("connection_failure", "Critical", "localhost")
    assert a.recommendation == RUNBOOK["connection_failure"]
    assert rule_connection(_health(True), S) == []


def test_database_state_access_and_log_rules():
    alerts = rule_databases(_health(rows=[
        ("Ok", "ONLINE", "MULTI_USER", 10),
        ("Off", "OFFLINE", "MULTI_USER", None),
        ("Single", "ONLINE", "SINGLE_USER", 5),
        ("LogWarn", "ONLINE", "MULTI_USER", 80),
        ("LogCrit", "ONLINE", "MULTI_USER", 95),
    ]), S)
    found = {(a.rule, a.target): a.severity for a in alerts}
    assert found == {("database_not_online", "Off"): "Critical",
                     ("database_access_restricted", "Single"): "Warning",
                     ("log_space", "LogWarn"): "Warning",
                     ("log_space", "LogCrit"): "Critical"}


def test_backup_rules_missing_and_overdue():
    dbs = pd.DataFrame([
        {"database_name": "NoFull", "state_desc": "ONLINE", "recovery_model_desc": "SIMPLE",
         "log_reuse_wait_desc": "NOTHING", "is_system": 0, "last_full_size_mb": None,
         "full_age_minutes": None, "diff_age_minutes": None, "log_age_minutes": None},
        {"database_name": "LogLate", "state_desc": "ONLINE", "recovery_model_desc": "FULL",
         "log_reuse_wait_desc": "NOTHING", "is_system": 0, "last_full_size_mb": 5,
         "full_age_minutes": 120, "diff_age_minutes": None, "log_age_minutes": 90},
        {"database_name": "Fine", "state_desc": "ONLINE", "recovery_model_desc": "FULL",
         "log_reuse_wait_desc": "NOTHING", "is_system": 0, "last_full_size_mb": 5,
         "full_age_minutes": 60, "diff_age_minutes": None, "log_age_minutes": 5},
    ])
    alerts = rule_backups(BackupSnapshot(True, NOW, databases=dbs), BackupPolicy())
    found = {(a.rule, a.target): a.severity for a in alerts}
    assert found == {("backup_missing", "NoFull"): "Critical", ("backup_overdue", "LogLate"): "Warning"}


def _blocking_rows(wait_ms):
    return [
        (61, "u1", "pc", "AppA", "Demo", "sleeping", 1, 0, 0, None, 0, None, None, None, None,
         "UPDATE t SET x = 1", 40),
        (62, "u2", "pc", "AppB", "Demo", "running", 1, 1, 61, "LCK_M_S", wait_ms, "KEY", 1, wait_ms, 1,
         "SELECT * FROM t", 0),
        (63, "u3", "pc", "AppC", "Demo", "running", 0, 1, 62, "LCK_M_S", wait_ms - 1000, "KEY", 1,
         wait_ms - 1000, 1, "SELECT * FROM t2", 0),
        (70, "u4", "pc", "Report", "Demo", "running", 0, 1, 0, None, 0, None, 50_000, 120_000, 9,
         "SELECT big report", 0),                                   # long-running, not blocked
        (71, "me", "pc", S.app_name, "master", "running", 0, 1, 0, None, 0, None, 1, 999_000, 1,
         "/* DBMonitor */ SELECT", 0),                              # the dashboard itself
    ]


def test_blocking_one_alert_per_head_blocker_and_prolonged_is_critical():
    short = [a for a in rule_sessions(_sessions(_blocking_rows(10_000)), S) if a.rule == "blocking"]
    assert len(short) == 1 and short[0].severity == "Warning" and short[0].target == "session 61"
    assert "61 → 62 → 63" in short[0].description
    long_ = [a for a in rule_sessions(_sessions(_blocking_rows(45_000)), S) if a.rule == "blocking"]
    assert long_[0].severity == "Critical" and long_[0].name == "Prolonged blocking"


def test_long_running_ignores_blocked_sessions_and_the_dashboard():
    alerts = [a for a in rule_sessions(_sessions(_blocking_rows(45_000)), S) if a.rule == "long_running"]
    assert [a.target for a in alerts] == ["session 70"]           # not 62/63 (blocked), not 71 (us)


def test_memory_rule():
    assert rule_memory(MemoryInfo(0), S) == []
    a = rule_memory(MemoryInfo(3, 2, 250, 2900), S)[0]
    assert a.severity == "Warning" and "3 Windows low-memory notification" in a.description


def test_collection_failure_suppressed_when_server_down():
    errors = {"Backups": "permission denied", "Sessions & blocking": None}
    assert [a.target for a in rule_collection(errors, _health(True), S)] == ["Backups"]
    assert rule_collection(errors, _health(False), S) == []          # connection alert covers it


def test_evaluate_rules_deduplicates_keys():
    health = _health(rows=[("Off", "OFFLINE", "MULTI_USER", None)])
    empty_sessions = SessionsSnapshot(True, NOW)
    empty_backups = BackupSnapshot(True, NOW)
    alerts = evaluate_rules(health, empty_sessions, empty_backups, MemoryInfo(), S)
    assert len({a.key for a in alerts}) == len(alerts)


# --- Alert store: no duplicates, resolve, re-open, purge ------------------------------

def _cand(rule="backup_missing", target="master", severity="Critical"):
    return AlertCandidate(rule, "No recorded full backup", severity, target, "desc", ["step 1", "step 2"])


def test_store_refresh_does_not_duplicate(tmp_path):
    store = AlertStore(tmp_path / "a.db")
    assert store.sync([_cand()], NOW)["opened"] == 1
    for i in range(1, 6):                                            # five refreshes
        assert store.sync([_cand()], NOW + timedelta(seconds=30 * i))["updated"] == 1
    active = store.frame("ACTIVE")
    assert len(active) == 1
    assert active.iloc[0]["checks_seen"] == 6
    assert active.iloc[0]["first_seen"] == pd.Timestamp(NOW)         # detection time unchanged


def test_store_resolves_and_reopens(tmp_path):
    store = AlertStore(tmp_path / "a.db")
    store.sync([_cand()], NOW)
    assert store.sync([], NOW + timedelta(minutes=1))["resolved"] == 1
    assert store.frame("ACTIVE").empty and len(store.frame("RESOLVED")) == 1
    later = NOW + timedelta(minutes=5)
    assert store.sync([_cand()], later)["reopened"] == 1
    row = store.frame("ACTIVE").iloc[0]
    assert row["times_opened"] == 2 and row["first_seen"] == pd.Timestamp(later)


def test_store_purges_old_resolved_alerts(tmp_path):
    store = AlertStore(tmp_path / "a.db")
    store.sync([_cand()], NOW)
    store.sync([], NOW + timedelta(minutes=1))
    assert store.sync([], NOW + timedelta(days=8), history_days=7)["purged"] == 1
    assert store.frame().empty


def test_summarize_alerts(tmp_path):
    store = AlertStore(tmp_path / "a.db")
    store.sync([_cand(), _cand("log_space", "db1", "Warning"), _cand("log_space", "db2", "Warning")], NOW)
    assert summarize_alerts(store.frame("ACTIVE")) == {"active": 3, "critical": 1, "warning": 2, "info": 0}


# --- Integration ------------------------------------------------------------------------

@pytest.mark.integration
def test_tc09_memory_query_and_full_evaluation_on_real_server():
    from monitor.collectors import collect_backups, collect_health, collect_sessions
    s = get_settings()
    memory, error = collect_memory(s)
    assert error is None and memory is not None and memory.low_memory_events >= 0
    backups = collect_backups(s)
    alerts = evaluate_rules(collect_health(s), collect_sessions(s), backups, memory, s)
    keys = {a.key for a in alerts}
    # Every ONLINE database without a full backup in msdb must have a "missing backup" alert,
    # and no database WITH a full backup may have one (expected list comes from msdb itself).
    dbs = backups.databases
    never = set(dbs.loc[dbs["last_full"].isna() & (dbs["state_desc"] == "ONLINE"), "database_name"])
    missing_alerted = {k.split("|", 1)[1] for k in keys if k.startswith("backup_missing|")}
    assert missing_alerted == never
    assert all(a.recommendation for a in alerts)


@pytest.mark.integration
def test_tc02_unreachable_server_gives_single_connection_alert():
    from monitor.collectors import collect_backups, collect_health, collect_sessions
    bad = replace(get_settings(), server="no-such-server.invalid", login_timeout_seconds=3)
    alerts = evaluate_rules(collect_health(bad), collect_sessions(bad), collect_backups(bad), None, bad)
    assert [a.rule for a in alerts] == ["connection_failure"]       # no duplicate per-module noise
