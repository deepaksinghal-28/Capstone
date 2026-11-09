"""
Integration tests - run against the REAL local SQL Server instance.

Test cases covered (capstone test plan):
  TC-01  Successful SQL Server connection
  TC-02  SQL Server connectivity failure is reported, not crashed
  TC-03  Database health status from sys.databases / sys.master_files

Run (PowerShell, in the project folder, SQL Server must be running):
    & "$env:USERPROFILE\\venvs\\capstone\\Scripts\\python.exe" -m pytest tests -m integration -v
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from monitor.collectors import collect_health
from monitor.config import get_settings
from monitor.db import run_query

pytestmark = pytest.mark.integration


def test_tc01_successful_connection():
    df = run_query("SELECT SUSER_SNAME() AS login_name, @@SERVERNAME AS server_name;")
    assert len(df) == 1
    assert df.loc[0, "login_name"]                      # Windows login is returned


def test_tc01_health_snapshot_has_server_info():
    snap = collect_health()
    assert snap.connected, snap.error
    srv = snap.server
    assert srv["product_version"].startswith("16.")    # SQL Server 2022
    assert "Edition" in srv["edition"]
    assert srv["uptime_seconds"] >= 0
    assert srv["cpu_count"] >= 1
    assert snap.collected_at_server is not None


def test_tc02_connection_failure_is_friendly():
    bad = replace(get_settings(), server="no-such-server.invalid", login_timeout_seconds=3)
    snap = collect_health(bad)
    assert snap.connected is False
    assert "Cannot reach SQL Server" in snap.error
    assert snap.databases.empty


def test_tc02_wrong_driver_is_friendly():
    bad = replace(get_settings(), driver="ODBC Driver 99 for SQL Server")
    snap = collect_health(bad)
    assert snap.connected is False
    assert "ODBC driver" in snap.error


def test_tc03_system_databases_listed_and_online():
    snap = collect_health()
    assert snap.connected, snap.error
    dbs = snap.databases.set_index("database_name")
    for name in ("master", "model", "msdb", "tempdb"):
        assert name in dbs.index
        assert dbs.loc[name, "state_desc"] == "ONLINE"
        assert dbs.loc[name, "total_size_mb"] > 0
    assert set(snap.databases["health_status"]) <= {"Healthy", "Warning", "Critical"}


def test_tc03_sizes_match_sys_master_files():
    snap = collect_health()
    expected = run_query("SELECT SUM(CAST(size AS bigint)) * 8 / 1024.0 AS mb FROM sys.master_files;")
    assert snap.databases["total_size_mb"].sum() == pytest.approx(float(expected.loc[0, "mb"]), abs=0.05)
