"""
Multi-instance tests: server list parsing, per-server alert keys, alert-store
migration, and live checks against BOTH local instances.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from monitor.alerts import AlertCandidate, AlertStore
from monitor.collectors import collect_backups, collect_health, collect_sessions
from monitor.config import (ServerTarget, exact_server_match, get_settings, parse_servers,
                            search_servers, server_aliases, server_targets, settings_for)

NOW = datetime(2026, 11, 9, 19, 0, 0)
BOTH = r"Primary=localhost;Secondary=localhost\SQLDEV2"


# --- Server list --------------------------------------------------------------------

def test_parse_servers_names_and_fallbacks():
    assert parse_servers(BOTH) == (ServerTarget("Primary", "localhost"),
                                   ServerTarget("Secondary", r"localhost\SQLDEV2"))
    assert parse_servers(" localhost ; ;Dup=a;dup=b") == (ServerTarget("localhost", "localhost"),
                                                          ServerTarget("Dup", "a"))
    assert parse_servers("") == ()


def test_server_targets_falls_back_to_sql_server():
    s = get_settings()
    from dataclasses import replace
    single = replace(s, servers=(), server="localhost")
    assert server_targets(single) == (ServerTarget("localhost", "localhost"),)
    assert settings_for(single, r"localhost\SQLDEV2").server == r"localhost\SQLDEV2"


def test_default_alert_store_is_outside_appdata_and_onedrive():
    from dataclasses import replace

    from monitor.alerts import default_store_path
    path = default_store_path(replace(get_settings(), alert_store_path=""))
    text = str(path).lower()
    assert "appdata" not in text and "onedrive" not in text
    assert path.name == "alerts.db"


def test_search_servers_ranks_and_ignores_case():
    many = parse_servers("Primary=localhost;Secondary=localhost\\SQLDEV2;"
                         "SQLDEV2-Archive=archive01\\SQLDEV2B;Payroll=pay01")
    assert [t.name for t in search_servers(many, "sqldev2")] == ["Secondary", "SQLDEV2-Archive"]
    assert [t.name for t in search_servers(many, "PAY")] == ["Payroll"]
    assert [t.name for t in search_servers(many, "local")] == ["Primary", "Secondary"]
    assert search_servers(many, "") == [] and search_servers(many, "nothing") == []


def test_search_by_real_computer_name(monkeypatch):
    monkeypatch.setenv("COMPUTERNAME", "SQLHOST-01")
    both = parse_servers(BOTH)
    assert server_aliases(both[0]) == ["primary", "localhost", "sqlhost-01"]
    assert "sqlhost-01\\sqldev2" in server_aliases(both[1])
    # the computer name matches both (Secondary is SQLHOST-01\SQLDEV2) ...
    assert [t.name for t in search_servers(both, "SQLHOST-01")] == ["Primary", "Secondary"]
    # ... but only Primary is EXACTLY that name, so it opens directly
    assert exact_server_match(both, "sqlhost-01").name == "Primary"
    assert exact_server_match(both, "SQLHOST-01\\SQLDEV2").name == "Secondary"
    assert exact_server_match(both, "cpc") is None                  # partial -> pick from list
    # remote servers: the connection name already is the host name, no alias added
    remote = ServerTarget("Payroll", "sqlprd01.corp.local")
    assert server_aliases(remote) == ["payroll", "sqlprd01.corp.local"]


def test_search_servers_is_fast_for_thousands():
    import time
    fleet = tuple(ServerTarget(f"SQLPRD{i:04d}", f"sqlprd{i:04d}.corp.local") for i in range(5000))
    start = time.perf_counter()
    found = search_servers(fleet, "SQLPRD4321")
    assert [t.name for t in found] == ["SQLPRD4321"]
    assert time.perf_counter() - start < 0.5                  # list search only, no server contacted


def test_target_label():
    assert ServerTarget("Secondary", r"localhost\SQLDEV2").label == r"Secondary · localhost\SQLDEV2"
    assert ServerTarget("localhost", "localhost").label == "localhost"


# --- Alerts per server --------------------------------------------------------------------

def _cand(server):
    return AlertCandidate("backup_missing", "No recorded full backup", "Critical", "model",
                          "desc", ["step"], server=server)


def test_same_problem_on_two_servers_is_two_alerts(tmp_path):
    store = AlertStore(tmp_path / "a.db")
    store.sync([_cand("Primary"), _cand("Secondary")], NOW)
    active = store.frame("ACTIVE")
    assert len(active) == 2
    assert set(active["alert_key"]) == {"Primary|backup_missing|model", "Secondary|backup_missing|model"}
    assert set(active["server"]) == {"Primary", "Secondary"}


def test_legacy_single_server_store_is_migrated_without_duplicates(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:                     # store created by the single-server version
        conn.executescript("""
            CREATE TABLE alerts (alert_key TEXT PRIMARY KEY, rule TEXT NOT NULL, name TEXT NOT NULL,
              severity TEXT NOT NULL, target TEXT NOT NULL, description TEXT NOT NULL,
              recommendation TEXT NOT NULL, status TEXT NOT NULL, first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL, resolved_at TEXT, checks_seen INTEGER NOT NULL DEFAULT 1,
              times_opened INTEGER NOT NULL DEFAULT 1);
            INSERT INTO alerts VALUES ('backup_missing|model', 'backup_missing', 'No recorded full backup',
              'Critical', 'model', 'desc', 'step', 'ACTIVE', '2026-11-09T18:21:43',
              '2026-11-09T18:22:10', NULL, 3, 1);""")
    conn.close()
    store = AlertStore(path, legacy_server="Primary")
    counts = store.sync([_cand("Primary")], NOW)
    assert counts["updated"] == 1 and counts["opened"] == 0          # continued, not duplicated
    row = store.frame("ACTIVE").iloc[0]
    assert row["alert_key"] == "Primary|backup_missing|model"
    assert row["checks_seen"] == 4
    assert str(row["first_seen"]) == "2026-11-09 18:21:43"            # original detection time kept


# --- Live: both instances ---------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize("target", parse_servers(BOTH), ids=lambda t: t.name)
def test_every_module_collects_from_each_instance(target):
    s = settings_for(get_settings(), target.server)
    health = collect_health(s)
    assert health.connected, health.error
    assert health.server["product_version"] == "16.0.4305.5"
    expected_service = "SQLDEV2" if "\\" in target.server else None
    assert (health.server["server_name"].endswith("\\SQLDEV2")) == bool(expected_service)
    assert collect_sessions(s).connected
    backups = collect_backups(s)
    assert backups.connected and {"master", "msdb"} <= set(backups.databases["database_name"])
