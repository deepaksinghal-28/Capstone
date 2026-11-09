"""
Generate a controlled, READ-ONLY query workload in the CapstoneDemo test
database so the Query Performance page has real numbers to show.

It simulates an application calling stored procedures with random parameters:
    usp_GetCustomerOrders     cheap index seek            (called most often)
    usp_FindCustomersByEmail  leading-wildcard LIKE scan
    usp_OrdersByStatus        full scan, no index on Status
    usp_SalesByCity           join + aggregate
    usp_ProductRevenueReport  CPU-heavy aggregate + window functions
plus one ad-hoc parameterised query.

Only SELECT statements are executed - no data is changed.

Usage (PowerShell, project folder):
    & "$env:USERPROFILE\\venvs\\capstone\\Scripts\\python.exe" tools\\generate_query_workload.py --seconds 60
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter
from pathlib import Path

import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor.config import get_settings          # noqa: E402
from monitor.db import build_connection_string   # noqa: E402

# (label, SQL, parameter factory, weight)
WORKLOAD = [
    ("usp_GetCustomerOrders", "EXEC dbo.usp_GetCustomerOrders @CustomerID = ?",
     lambda: [random.randint(1, 20000)], 60),
    ("usp_FindCustomersByEmail", "EXEC dbo.usp_FindCustomersByEmail @Fragment = ?",
     lambda: [str(random.randint(100, 999))], 15),
    ("usp_OrdersByStatus", "EXEC dbo.usp_OrdersByStatus @Status = ?",
     lambda: [random.choice(["New", "Shipped", "Delivered", "Cancelled"])], 12),
    ("usp_SalesByCity", "EXEC dbo.usp_SalesByCity @FromDate = ?",
     lambda: [f"2026-{random.randint(1, 10):02d}-01"], 8),
    ("usp_ProductRevenueReport", "EXEC dbo.usp_ProductRevenueReport", lambda: [], 3),
    ("ad-hoc top customers",
     "SELECT TOP (10) CustomerID, SUM(Amount) AS Total FROM dbo.Orders "
     "WHERE OrderDate >= ? GROUP BY CustomerID ORDER BY Total DESC",
     lambda: [f"2026-{random.randint(6, 10):02d}-01"], 2),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=int, default=60, help="how long to run (default 60)")
    args = parser.parse_args()

    settings = get_settings()
    conn_str = build_connection_string(settings).replace(
        f"DATABASE={settings.database}", "DATABASE=CapstoneDemo").replace(
        f"APP={settings.app_name}", "APP=Capstone-DemoWorkload")
    try:
        conn = pyodbc.connect(conn_str, timeout=settings.login_timeout_seconds, autocommit=True)
    except pyodbc.Error as exc:
        print(f"Cannot connect to CapstoneDemo: {exc}")
        print("Create it first with sql\\demo\\01_create_capstone_demo.sql")
        return 1

    cursor = conn.cursor()
    labels = [w[0] for w in WORKLOAD]
    weights = [w[3] for w in WORKLOAD]
    by_label = {w[0]: w for w in WORKLOAD}
    counts: Counter[str] = Counter()
    deadline = time.monotonic() + args.seconds
    print(f"Running demo workload against CapstoneDemo for {args.seconds} s (read-only)…")
    last_report = time.monotonic()
    try:
        while time.monotonic() < deadline:
            label = random.choices(labels, weights)[0]
            _, sql, params, _ = by_label[label]
            cursor.execute(sql, params())
            while True:                       # consume every result set
                if cursor.description:
                    cursor.fetchall()
                if not cursor.nextset():
                    break
            counts[label] += 1
            if time.monotonic() - last_report >= 10:
                print(f"  {sum(counts.values()):,} calls so far…")
                last_report = time.monotonic()
    except KeyboardInterrupt:
        print("Stopped by user.")
    finally:
        conn.close()

    print("\nCalls executed:")
    for label in labels:
        print(f"  {label:<28} {counts[label]:>6,}")
    print(f"  {'TOTAL':<28} {sum(counts.values()):>6,}")
    print("\nOpen the Query Performance page and press 'Refresh now'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
