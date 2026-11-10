"""
Performance-history collector (Module 6).

Saves one snapshot of every monitored server (SQL_SERVERS in .env) every
HISTORY_INTERVAL_SECONDS (default 300 s = 5 min) into history.db, and checks the
alert rules each time - so history and alerts keep being recorded even while
nobody has the dashboard open.

In between, every BLOCKING_CHECK_SECONDS (default 60 s) it reads only the session
DMVs: a session blocked for BLOCKING_DETAIL_SECONDS (default 300 s) or longer is saved
with the blocked statement and the head blocker (login, host, program, last statement).

run_app.ps1 starts this automatically next to the dashboard. Manually:
    & "$env:USERPROFILE\\venvs\\capstone\\Scripts\\python.exe" tools\\collect_history.py
    ...collect_history.py --once          one snapshot, then exit (e.g. Windows Task Scheduler)
    ...collect_history.py --interval 60   every minute (minimum 60 s)

Read-only on SQL Server: the same DMV queries as the dashboard pages.
Press Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor.config import get_settings, server_targets   # noqa: E402
from monitor.history import HistoryStore, default_history_path, run_once, watch_blocking   # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Save a performance snapshot of every monitored server.")
    parser.add_argument("--once", action="store_true", help="collect one snapshot and exit")
    parser.add_argument("--interval", type=int, default=None,
                        help="seconds between snapshots (default HISTORY_INTERVAL_SECONDS, 300)")
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.demo_mode:
        print("Demo mode is on (DEMO_MODE=true or no SQL Server ODBC driver): the demo shows "
              "generated sample history, so there is nothing to collect.")
        return 0
    if hasattr(sys.stdout, "reconfigure"):      # the log file may not be UTF-8 by default
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    interval = max(60, args.interval or settings.history_interval_seconds)
    check = max(15, min(settings.blocking_check_seconds, interval))
    store = HistoryStore(default_history_path(settings))
    names = ", ".join(t.name for t in server_targets(settings))
    print(f"History collector: {names} - snapshot every {interval} s, blocking check every {check} s "
          f"(records blocks >= {settings.blocking_detail_seconds} s) -> {store.path}  (Ctrl+C stops)",
          flush=True)

    try:
        next_snapshot = time.monotonic()
        while True:
            started = time.monotonic()
            try:
                if started >= next_snapshot:          # full snapshot (includes the blocking check)
                    next_snapshot = started + interval
                    lines = run_once(settings, store)
                else:                                 # quick sessions-only check
                    lines = watch_blocking(settings, store)
                for line in lines:
                    print(line, flush=True)
            except Exception as exc:        # one bad cycle must not stop the collector
                print(f"{datetime.now():%H:%M} collection failed: {exc}", flush=True)
            if args.once:
                return 0
            time.sleep(max(1.0, min(next_snapshot, time.monotonic() + check) - time.monotonic()))
    except KeyboardInterrupt:
        print("History collector stopped.", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
