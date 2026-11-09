"""
Run a T-SQL script file against the local SQL Server - like pressing F5 in SSMS.

The script is split on lines containing only "GO" (exactly as SSMS does) and
each batch is executed in order. PRINT / RAISERROR messages and result sets are
shown. Execution STOPS at the first error.

Usage (PowerShell, project folder):
    & "$env:USERPROFILE\\venvs\\capstone\\Scripts\\python.exe" tools\\run_sql_script.py sql\\demo\\01_create_capstone_demo.sql
    ...run_sql_script.py sql\\demo\\05_create_sqldev2_salesarchive.sql --server localhost\\SQLDEV2

Uses the same .env connection settings as the dashboard (Windows Authentication).
Without --server the script runs on SQL_SERVER from .env (localhost).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor.config import get_settings, settings_for   # noqa: E402
from monitor.db import build_connection_string   # noqa: E402

GO_LINE = re.compile(r"^\s*GO\s*(?:--.*)?$", re.IGNORECASE | re.MULTILINE)


def split_batches(script: str) -> list[str]:
    """Split a script on GO separator lines; drop empty batches."""
    return [b.strip() for b in GO_LINE.split(script) if b.strip()]


def print_messages(cursor: pyodbc.Cursor) -> None:
    for _, text in getattr(cursor, "messages", None) or []:
        # strip the ODBC driver prefix: [Microsoft][ODBC Driver 17 ...][SQL Server]
        print("   ", re.sub(r"^(\[[^\]]*\])+", "", str(text)))


def run_script(path: Path, server: str | None = None) -> int:
    script = path.read_text(encoding="utf-8-sig")
    batches = split_batches(script)
    settings = get_settings()
    if server:
        settings = settings_for(settings, server)
    print(f"Server : {settings.server}  (Windows Authentication)")
    print(f"Script : {path}  ({len(batches)} batches)\n")

    conn = pyodbc.connect(build_connection_string(settings).replace(
        f"APP={settings.app_name}", "APP=Capstone-ScriptRunner"),
        timeout=settings.login_timeout_seconds, autocommit=True)
    conn.timeout = 0          # no query timeout: data generation can take a while
    cursor = conn.cursor()
    try:
        for number, batch in enumerate(batches, start=1):
            first_line = next((ln for ln in batch.splitlines()
                               if ln.strip() and not ln.strip().startswith(("--", "/*", "*"))), "")
            print(f"[{number}/{len(batches)}] {first_line.strip()[:70]}")
            try:
                cursor.execute(batch)
                while True:
                    print_messages(cursor)
                    if cursor.description:
                        cols = [c[0] for c in cursor.description]
                        for row in cursor.fetchall():
                            print("    " + ", ".join(f"{c}={v}" for c, v in zip(cols, row)))
                    if not cursor.nextset():
                        break
            except pyodbc.Error as exc:
                print_messages(cursor)
                message = exc.args[1] if len(exc.args) > 1 else str(exc)
                print(f"\nERROR in batch {number}: {re.sub(r'^(\[[^\]]*\])+', '', message)}")
                print("Execution stopped. Batches after this one were NOT run.")
                return 1
    finally:
        conn.close()
    print("\nCompleted: all batches executed.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a .sql file like SSMS F5 (splits on GO).")
    parser.add_argument("script", type=Path, help="path of the .sql file")
    parser.add_argument("--server", help=r"instance to run on, e.g. localhost\SQLDEV2 "
                                         "(default: SQL_SERVER from .env)")
    args = parser.parse_args()
    sys.exit(run_script(args.script, args.server))
