"""
Database connectivity.

DBA analogy:
  * connect()   = clicking "Connect" in SSMS with Windows Authentication
  * run_query() = running a SELECT in a query window and getting the grid back
                  (here the "grid" is a pandas DataFrame)

Safety:
  * Windows Authentication only (Trusted_Connection=yes) - no passwords.
  * autocommit is ON, so the monitor can never leave an open transaction
    holding locks. All monitoring queries are read-only SELECTs.
  * A query timeout stops a slow DMV query from hanging the dashboard.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Sequence

import pandas as pd

try:
    import pyodbc
except ImportError:          # e.g. a cloud host for the public demo without an ODBC driver manager
    pyodbc = None

from monitor.config import Settings, get_settings


# Every query this application sends starts with this comment, so the dashboard
# can recognise (and hide) its own monitoring queries in the plan cache, and a
# DBA can spot them in sp_WhoIsActive / Profiler / Extended Events.
MONITOR_MARKER = "/* DBMonitor */"


class DatabaseError(Exception):
    """A SQL Server problem, with a message that is safe to show on screen."""

    def __init__(self, friendly_message: str, technical_detail: str = "") -> None:
        super().__init__(friendly_message)
        self.friendly_message = friendly_message
        self.technical_detail = technical_detail


def build_connection_string(settings: Settings) -> str:
    """Return the ODBC connection string (equivalent of the SSMS connect dialog)."""
    parts = {
        "DRIVER": "{" + settings.driver + "}",
        "SERVER": settings.server,
        "DATABASE": settings.database,
        "Trusted_Connection": "yes",          # Windows Authentication
        "Encrypt": "yes" if settings.encrypt else "no",
        "TrustServerCertificate": "yes" if settings.trust_server_certificate else "no",
        "APP": settings.app_name,             # shows as program_name in SQL Server
    }
    return ";".join(f"{key}={value}" for key, value in parts.items()) + ";"


def friendly_error(exc: Exception, settings: Settings | None = None) -> DatabaseError:
    """Translate a pyodbc error into a plain-English message for the dashboard."""
    settings = settings or get_settings()
    sqlstate = ""
    detail = str(exc)
    if pyodbc is not None and isinstance(exc, pyodbc.Error) and exc.args:
        sqlstate = str(exc.args[0])
        if len(exc.args) > 1:
            detail = str(exc.args[1])
    lowered = detail.lower()

    if sqlstate == "IM002" or "data source name not found" in lowered:
        msg = (f"ODBC driver '{settings.driver}' is not installed or the name is wrong. "
               "Check SQL_ODBC_DRIVER in the .env file.")
    elif "query timeout" in lowered:
        msg = "A monitoring query took too long and was cancelled (query timeout)."
    elif sqlstate in {"08001", "HYT00", "08S01"} or "server does not exist" in lowered \
            or "login timeout" in lowered or "could not open a connection" in lowered:
        msg = (f"Cannot reach SQL Server '{settings.server}'. Check that the SQL Server "
               "(MSSQLSERVER) service is running and that SQL_SERVER in .env is correct.")
    elif sqlstate == "28000" or "login failed" in lowered:
        msg = ("SQL Server rejected the Windows login. Ask the DBA to create a login for "
               "your Windows account and grant VIEW SERVER STATE.")
    elif "permission" in lowered or "denied" in lowered:
        msg = ("Connected, but this login lacks permission for a monitoring view. "
               "Grant VIEW SERVER STATE (and read access to msdb for backup history).")
    else:
        msg = "Unexpected SQL Server error while collecting monitoring data."
    return DatabaseError(msg, f"[{sqlstate}] {detail}" if sqlstate else detail)


@contextmanager
def connect(settings: Settings | None = None) -> Iterator[pyodbc.Connection]:
    """Open a read-only-style monitoring connection and always close it afterwards."""
    settings = settings or get_settings()
    if settings.demo_mode:
        raise DatabaseError("Demo mode: no SQL Server connection is made.")
    if pyodbc is None:
        raise DatabaseError("pyodbc / the ODBC driver manager is not installed on this machine.")
    try:
        conn = pyodbc.connect(
            build_connection_string(settings),
            timeout=settings.login_timeout_seconds,   # login timeout
            autocommit=True,                          # never hold a transaction open
        )
    except pyodbc.Error as exc:
        raise friendly_error(exc, settings) from exc
    conn.timeout = settings.query_timeout_seconds     # per-query timeout
    try:
        yield conn
    finally:
        conn.close()


def query_df(conn: pyodbc.Connection, sql: str, params: Sequence | None = None,
             settings: Settings | None = None) -> pd.DataFrame:
    """Run one SELECT on an open connection and return the result grid as a DataFrame."""
    try:
        cursor = conn.cursor()
        cursor.execute(f"{MONITOR_MARKER}\n{sql}", params or [])
        # Skip "rows affected"/empty results until we reach the SELECT result set.
        while cursor.description is None and cursor.nextset():
            pass
        if cursor.description is None:
            return pd.DataFrame()
        columns = [col[0] for col in cursor.description]
        rows = [tuple(row) for row in cursor.fetchall()]
        return pd.DataFrame.from_records(rows, columns=columns)
    except pyodbc.Error as exc:
        raise friendly_error(exc, settings) from exc


def run_query(sql: str, params: Sequence | None = None,
              settings: Settings | None = None) -> pd.DataFrame:
    """Convenience: connect, run one query, disconnect."""
    settings = settings or get_settings()
    with connect(settings) as conn:
        return query_df(conn, sql, params, settings)
