"""
Controlled, self-releasing BLOCKING demonstration in the CapstoneDemo test database.

What it does (3 separate SQL Server sessions):

    Session A  "Capstone-Blocking-Head"
        BEGIN TRAN; UPDATE dbo.BlockingDemo ... WHERE Id = 1;   -> holds an X lock on row 1
        then sits idle with the transaction OPEN (classic "idle head blocker")

    Session B  "Capstone-Blocking-Middle"
        BEGIN TRAN; UPDATE ... WHERE Id = 2;                     -> holds an X lock on row 2
        SELECT ... WHERE Id = 1;                                 -> BLOCKED by A (LCK_M_S wait)

    Session C  "Capstone-Blocking-Tail"
        SELECT ... WHERE Id = 2;                                 -> BLOCKED by B

    Chain shown in the dashboard:  A → B → C

How it is released (no KILL needed):
    After --seconds (default 90) session A runs ROLLBACK. B's SELECT then completes
    and B runs ROLLBACK, which releases C. Nothing is ever committed.
    Ctrl+C also rolls back immediately. If this program crashes, SQL Server rolls the
    transactions back automatically when the connections close.
    Safety net: B and C use LOCK_TIMEOUT and a query timeout so they can never wait forever.

Usage (PowerShell, project folder):
    & "$env:USERPROFILE\\venvs\\capstone\\Scripts\\python.exe" tools\\blocking_demo.py --seconds 90
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor.config import get_settings          # noqa: E402
from monitor.db import build_connection_string   # noqa: E402


def open_session(app: str, timeout_s: int) -> pyodbc.Connection:
    s = get_settings()
    cs = (build_connection_string(s)
          .replace(f"DATABASE={s.database}", "DATABASE=CapstoneDemo")
          .replace(f"APP={s.app_name}", f"APP={app}"))
    conn = pyodbc.connect(cs, timeout=s.login_timeout_seconds, autocommit=True)
    conn.timeout = timeout_s
    return conn


def spid(conn: pyodbc.Connection) -> int:
    return int(conn.cursor().execute("SELECT @@SPID").fetchval())


def waiter(name: str, conn: pyodbc.Connection, statements: list[str], results: dict) -> None:
    """Run statements on a blocked session and record how it ended."""
    started = time.monotonic()
    cur = conn.cursor()
    try:
        for sql in statements:
            cur.execute(sql)
            while True:
                if cur.description:
                    cur.fetchall()
                if not cur.nextset():
                    break
        results[name] = f"completed after waiting {time.monotonic() - started:.0f} s"
    except pyodbc.Error as exc:
        results[name] = f"ended with error: {exc.args[-1] if exc.args else exc}"
    finally:
        try:
            cur.execute("IF @@TRANCOUNT > 0 ROLLBACK;")
        except pyodbc.Error:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=int, default=90,
                        help="how long session A holds its lock (10-600, default 90)")
    args = parser.parse_args()
    hold = max(10, min(args.seconds, 600))
    safety = hold + 60                                 # B/C give up after this

    try:
        a = open_session("Capstone-Blocking-Head", 30)
        a.cursor().execute("SELECT 1 FROM dbo.BlockingDemo WHERE Id = 1").fetchall()
    except pyodbc.Error as exc:
        print(f"Cannot use CapstoneDemo.dbo.BlockingDemo: {exc.args[-1] if exc.args else exc}")
        print("Run sql\\demo\\02_create_blocking_demo_table.sql first.")
        return 1

    b = open_session("Capstone-Blocking-Middle", safety)
    c = open_session("Capstone-Blocking-Tail", safety)
    ids = {"A": spid(a), "B": spid(b), "C": spid(c)}
    results: dict[str, str] = {}
    lock_timeout = f"SET LOCK_TIMEOUT {safety * 1000};"

    print(f"Blocking demo in CapstoneDemo - sessions A={ids['A']}, B={ids['B']}, C={ids['C']}")
    print(f"Expected chain on the dashboard:  {ids['A']} → {ids['B']} → {ids['C']}")
    print(f"Session A will hold its lock for {hold} s, then ROLLBACK (Ctrl+C = release now).\n")

    a_cur = a.cursor()
    try:
        a_cur.execute("BEGIN TRANSACTION; "
                      "UPDATE dbo.BlockingDemo SET Note = 'locked by demo session A', "
                      "UpdatedAt = SYSDATETIME() WHERE Id = 1;")
        tb = threading.Thread(target=waiter, daemon=True, args=("B", b, [
            lock_timeout,
            "BEGIN TRANSACTION; UPDATE dbo.BlockingDemo SET Note = 'locked by demo session B', "
            "UpdatedAt = SYSDATETIME() WHERE Id = 2;",
            "SELECT Note FROM dbo.BlockingDemo WHERE Id = 1;",            # blocked by A
        ], results))
        tb.start()
        time.sleep(1.5)                                                   # let B take row 2 first
        tc = threading.Thread(target=waiter, daemon=True, args=("C", c, [
            lock_timeout,
            "SELECT Note FROM dbo.BlockingDemo WHERE Id = 2;",            # blocked by B
        ], results))
        tc.start()

        deadline = time.monotonic() + hold
        while time.monotonic() < deadline:
            left = int(deadline - time.monotonic())
            print(f"  blocking active - releasing in {left:>3} s  (open the Sessions & blocking page)")
            time.sleep(min(15, max(1, left)))
    except KeyboardInterrupt:
        print("\nCtrl+C - releasing now.")
    finally:
        try:
            a_cur.execute("IF @@TRANCOUNT > 0 ROLLBACK;")
            print("\nSession A: ROLLBACK done - row 1 unchanged, lock released.")
        except pyodbc.Error as exc:
            print(f"Session A rollback error (SQL Server will roll back on disconnect): {exc}")
        for t in (locals().get("tb"), locals().get("tc")):
            if t is not None:
                t.join(timeout=30)
        for conn in (a, b, c):
            try:
                conn.close()
            except pyodbc.Error:
                pass

    print(f"Session B: {results.get('B', 'still waiting when the demo ended')} (then ROLLBACK)")
    print(f"Session C: {results.get('C', 'still waiting when the demo ended')}")
    print("All demo transactions were rolled back; nothing was committed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
