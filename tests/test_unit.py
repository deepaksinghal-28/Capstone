"""
Unit tests - check the Python logic WITHOUT needing SQL Server.

Run (PowerShell, in the project folder):
    & "$env:USERPROFILE\\venvs\\capstone\\Scripts\\python.exe" -m pytest tests -m "not integration"
"""

from __future__ import annotations

import pandas as pd
import pyodbc
import pytest

from monitor.collectors import (format_size_mb, format_uptime, prepare_databases,
                                summarize_databases, version_label)
from monitor.config import Settings
from monitor.db import build_connection_string, friendly_error


# --- Connection string --------------------------------------------------------

def test_connection_string_uses_windows_auth_and_no_password():
    cs = build_connection_string(Settings())
    assert "Trusted_Connection=yes" in cs
    assert "DRIVER={ODBC Driver 17 for SQL Server}" in cs
    assert "SERVER=localhost" in cs
    assert "PWD" not in cs.upper()
    assert "UID=" not in cs.upper()


def test_connection_string_encrypt_flag():
    assert "Encrypt=yes" in build_connection_string(Settings(encrypt=True))
    assert "Encrypt=no" in build_connection_string(Settings(encrypt=False))


# --- Friendly error messages ----------------------------------------------------

@pytest.mark.parametrize("sqlstate, text, expected", [
    ("08001", "Named Pipes Provider: Could not open a connection", "Cannot reach SQL Server"),
    ("HYT00", "Login timeout expired", "Cannot reach SQL Server"),
    ("HYT00", "Query timeout expired", "query timeout"),
    ("28000", "Login failed for user 'X'", "rejected the Windows login"),
    ("IM002", "Data source name not found", "ODBC driver"),
    ("42000", "VIEW SERVER STATE permission was denied", "lacks permission"),
])
def test_friendly_error_mapping(sqlstate, text, expected):
    err = friendly_error(pyodbc.Error(sqlstate, text), Settings())
    assert expected.lower() in err.friendly_message.lower()
    assert sqlstate in err.technical_detail


# --- Formatting -------------------------------------------------------------------

@pytest.mark.parametrize("seconds, expected", [
    (45, "45s"), (366, "6m"), (3_720, "1h 2m"), (93_784, "1d 2h 3m"), (None, "n/a"), (-5, "n/a"),
])
def test_format_uptime(seconds, expected):
    assert format_uptime(seconds) == expected


@pytest.mark.parametrize("mb, expected", [
    (40.69, "40.7 MB"), (1536, "1.5 GB"), (2 * 1024 * 1024, "2.00 TB"), (None, "n/a"),
])
def test_format_size_mb(mb, expected):
    assert format_size_mb(mb) == expected


def test_version_label():
    assert version_label({"product_version": "16.0.4305.5",
                          "product_update_level": "CU27"}) == "SQL Server 2022 CU27"
    assert version_label({"product_version": "16.0.1000.6", "product_update_level": None,
                          "product_level": "RTM"}) == "SQL Server 2022 RTM"


# --- Database health rules -------------------------------------------------------

def _sample_databases() -> pd.DataFrame:
    return pd.DataFrame([
        # name,   state,     access,        system, data,  log,  total, log%
        ("master", "ONLINE",  "MULTI_USER",  1, "4.00", "1.25", "5.25", 30),
        ("SalesDB", "ONLINE", "MULTI_USER",  0, "100.0", "50.0", "150.0", 90),
        ("OldDB",  "OFFLINE", "MULTI_USER",  0, "10.0", "5.0", "15.0", None),
        ("HRDB",   "ONLINE",  "SINGLE_USER", 0, "20.0", "5.0", "25.0", 10),
    ], columns=["database_name", "state_desc", "user_access_desc", "is_system",
                "data_size_mb", "log_size_mb", "total_size_mb", "log_used_pct"])


def test_prepare_databases_health_status():
    df = prepare_databases(_sample_databases(), log_warning_pct=75)
    status = dict(zip(df["database_name"], df["health_status"]))
    assert status == {"master": "Healthy", "SalesDB": "Warning",
                      "OldDB": "Critical", "HRDB": "Warning"}
    assert df["total_size_mb"].dtype.kind == "f"     # Decimal/text converted to numbers


def test_summarize_databases():
    s = summarize_databases(prepare_databases(_sample_databases(), log_warning_pct=75))
    assert s["total"] == 4
    assert s["online"] == 3
    assert s["not_online"] == 1
    assert s["user"] == 3 and s["system"] == 1
    assert s["warnings"] == 2
    assert s["total_size_mb"] == pytest.approx(195.25)


def test_summarize_empty():
    assert summarize_databases(pd.DataFrame())["total"] == 0
