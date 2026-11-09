"""
SAMPLE data for DEMO_MODE - the public demo of the dashboard interface.

Everything here is FICTIONAL and generated in Python. No SQL Server is ever
contacted in demo mode. Every page shows a "Demo mode - sample data" banner.

The sample rows have exactly the same columns as the real DMV queries, and go
through the same preparation / rule functions, so the demo shows the real
dashboard behaviour (health status, rankings, blocking chains, backup policy,
alerts) - only the input is invented.

Demo fleet (see monitor.config.DEMO_TARGETS):
  DEMO-PROD-01    healthy databases, a live 3-session blocking chain, busy plan cache
  DEMO-PROD-02    transaction log almost full, log backups overdue, memory pressure
  DEMO-UAT-01     a database that has never been backed up
  DEMO-REPORT-01  one database OFFLINE, a long-running report query
  DEMO-DEV-01     unreachable (shows the "server unavailable" handling)
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pandas as pd

from monitor.collectors import (BackupSnapshot, HealthSnapshot, QueryPerfSnapshot, SessionsSnapshot,
                                blocking_chains, prepare_databases, prepare_query_stats,
                                prepare_sessions)
from monitor.config import Settings, get_settings

DEMO_NOTICE = "Demo mode - SAMPLE data, no real SQL Server is connected."

UNREACHABLE = {"demo-dev-01.example.local"}

# database name, recovery model, data MB, log MB, log used %, state
DATABASES: dict[str, list[tuple]] = {
    "demo-prod-01.example.local": [
        ("SalesDB", "FULL", 8_420.0, 1_024.0, 22, "ONLINE"),
        ("InventoryDB", "FULL", 3_150.5, 512.0, 9, "ONLINE"),
        ("CustomerDB", "FULL", 5_610.0, 768.0, 31, "ONLINE"),
        ("ReportingDW", "SIMPLE", 12_880.0, 2_048.0, 4, "ONLINE"),
    ],
    "demo-prod-02.example.local\\FINANCE": [
        ("FinanceDB", "FULL", 6_200.0, 4_096.0, 93, "ONLINE"),
        ("PayrollDB", "FULL", 1_480.0, 256.0, 18, "ONLINE"),
        ("AuditDB", "SIMPLE", 940.0, 128.0, 6, "ONLINE"),
    ],
    "demo-uat-01.example.local": [
        ("SalesDB_UAT", "SIMPLE", 2_100.0, 256.0, 12, "ONLINE"),
        ("NewFeatureDB", "FULL", 320.0, 64.0, 40, "ONLINE"),
    ],
    "demo-report-01.example.local": [
        ("ReportServer", "FULL", 450.0, 128.0, 15, "ONLINE"),
        ("ReportServerTempDB", "SIMPLE", 210.0, 64.0, 3, "ONLINE"),
        ("ArchiveOld", "SIMPLE", 3_300.0, 256.0, None, "OFFLINE"),
    ],
}

SYSTEM_DBS = [("master", "SIMPLE", 6.0, 2.0, 30, "ONLINE"), ("tempdb", "SIMPLE", 64.0, 16.0, 5, "ONLINE"),
              ("model", "FULL", 16.0, 8.0, 12, "ONLINE"), ("msdb", "SIMPLE", 48.0, 24.0, 8, "ONLINE")]

UPTIME_DAYS = {"demo-prod-01.example.local": 41.3, "demo-prod-02.example.local\\FINANCE": 12.8,
               "demo-uat-01.example.local": 3.2, "demo-report-01.example.local": 0.4}


def _rng(server: str, salt: str = "") -> random.Random:
    return random.Random(f"{server}|{salt}")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _unreachable(server: str) -> str:
    return (f"Cannot reach SQL Server '{server}'. Check that the SQL Server service is running "
            "and that the server name is correct. (sample error - demo mode)")


# ---------------------------------------------------------------------------
# Module 1 - health
# ---------------------------------------------------------------------------
def health(server: str, settings: Settings | None = None) -> HealthSnapshot:
    settings = settings or get_settings()
    now = _now()
    if server in UNREACHABLE:
        return HealthSnapshot(connected=False, collected_at_local=now, error=_unreachable(server),
                              error_detail="[08001] sample connection error (demo mode)")
    host = server.split("\\")[0].split(".")[0].upper()
    instance = server.split("\\")[1] if "\\" in server else None
    uptime = int(UPTIME_DAYS.get(server, 1.0) * 86_400)
    srv = {
        "server_name": host + (f"\\{instance}" if instance else ""),
        "machine_name": host,
        "edition": "Enterprise Edition: Core-based Licensing (64-bit)" if "prod" in server
        else "Developer Edition (64-bit)",
        "product_version": "16.0.4305.5", "product_level": "RTM", "product_update_level": "CU27",
        "product_update_reference": "KB5126934", "server_collation": "SQL_Latin1_General_CP1_CI_AS",
        "is_clustered": 0, "is_hadr_enabled": 0,
        "version_string": "Microsoft SQL Server 2022 (RTM-CU27-GDR) (KB5126934) - 16.0.4305.5 (X64) [demo]",
        "sqlserver_start_time": pd.Timestamp(now - timedelta(seconds=uptime)),
        "uptime_seconds": uptime,
        "cpu_count": 16 if "prod" in server else 8,
        "physical_memory_mb": 65_536 if "prod" in server else 32_768,
        "max_server_memory_mb": 57_344 if "prod" in server else 24_576,
        "sql_memory_in_use_mb": 55_900 if "prod" in server else 9_800,
        "server_time": pd.Timestamp(now),
        "server_utc_offset_minutes": 0,
    }
    rows = []
    for i, (name, model, data, log, used, state) in enumerate(SYSTEM_DBS + DATABASES.get(server, []),
                                                              start=1):
        rows.append({
            "database_id": i, "database_name": name, "state_desc": state, "user_access_desc": "MULTI_USER",
            "recovery_model_desc": model, "compatibility_level": 160, "is_read_only": 0,
            "is_system": 1 if i <= 4 else 0, "create_date": pd.Timestamp(now - timedelta(days=400 - i * 7)),
            "data_size_mb": data, "log_size_mb": log, "total_size_mb": data + log, "file_count": 2,
            "log_used_pct": used,
        })
    return HealthSnapshot(connected=True, collected_at_local=now, server=srv,
                          databases=prepare_databases(pd.DataFrame(rows), settings.log_used_warning_pct))


# ---------------------------------------------------------------------------
# Module 2 - query performance
# ---------------------------------------------------------------------------
QUERY_TEMPLATES = [
    ("usp_GetOrderHistory", "SELECT o.OrderID, o.OrderDate, o.Amount FROM dbo.Orders AS o "
                            "WHERE o.CustomerID = @CustomerID ORDER BY o.OrderDate DESC", 0.2, 4, 9000),
    ("usp_MonthlyRevenue", "WITH m AS (SELECT ProductID, DATEFROMPARTS(YEAR(OrderDate), MONTH(OrderDate), 1) "
                           "AS M, SUM(Amount) AS Rev FROM dbo.Orders GROUP BY ProductID, "
                           "DATEFROMPARTS(YEAR(OrderDate), MONTH(OrderDate), 1)) SELECT TOP (20) * FROM m "
                           "ORDER BY Rev DESC", 240.0, 21_000, 140),
    ("usp_SearchCustomers", "SELECT TOP (50) CustomerID, FullName, Email FROM dbo.Customers "
                            "WHERE Email LIKE N'%' + @Fragment + N'%'", 48.0, 3_100, 900),
    ("usp_OrdersByStatus", "SELECT COUNT(*), SUM(Amount) FROM dbo.Orders WHERE Status = @Status",
     36.0, 9_800, 700),
    ("usp_InventoryRecount", "UPDATE i SET OnHand = s.Qty FROM dbo.Inventory AS i JOIN staging.Stock AS s "
                             "ON s.ItemID = i.ItemID", 410.0, 52_000, 30),
    ("", "SELECT TOP (10) CustomerID, SUM(Amount) AS Total FROM dbo.Orders WHERE OrderDate >= @P1 "
         "GROUP BY CustomerID ORDER BY Total DESC", 31.0, 8_400, 260),
    ("usp_ClosePeriod", "EXEC dbo.usp_ClosePeriod @Period = @P1", 1_850.0, 310_000, 6),
]


def query_stats(server: str, metric: str = "cpu") -> QueryPerfSnapshot:
    now = _now()
    if server in UNREACHABLE:
        return QueryPerfSnapshot(connected=False, collected_at_local=now, metric=metric,
                                 error=_unreachable(server))
    rng = _rng(server, "queries")
    user_dbs = [d[0] for d in DATABASES.get(server, [("DemoDB",)]) if len(d) < 6 or d[5] == "ONLINE"]
    rows = []
    for n, (obj, text, avg_cpu_ms, avg_reads, execs) in enumerate(QUERY_TEMPLATES):
        execs = max(1, int(execs * rng.uniform(0.6, 1.4)))
        cpu_us = int(avg_cpu_ms * 1000 * execs * rng.uniform(0.8, 1.2))
        elapsed_us = int(cpu_us * rng.uniform(0.55, 1.6))      # < 1 = parallel plan
        rows.append({
            "query_hash": f"0x{rng.getrandbits(64):016X}", "execution_count": execs,
            "total_cpu_us": cpu_us, "total_elapsed_us": elapsed_us,
            "total_logical_reads": int(avg_reads * execs), "total_logical_writes": rng.randint(0, 900),
            "total_rows": execs * rng.randint(1, 40), "max_cpu_us": int(cpu_us / execs * 3),
            "max_elapsed_us": int(elapsed_us / execs * 4),
            "first_cached_time": pd.Timestamp(now - timedelta(hours=rng.uniform(2, 30))),
            "last_execution_time": pd.Timestamp(now - timedelta(seconds=rng.randint(1, 900))),
            "seconds_since_last_run": rng.randint(1, 900), "cached_plan_count": 1 if obj else 3,
            "database_name": user_dbs[n % len(user_dbs)], "object_name": obj or None,
            "is_monitor_query": 0, "query_text": text,
        })
    cache = {"cached_statements": 1_000 + rng.randint(0, 3_000), "distinct_query_shapes": 300 + rng.randint(0, 900),
             "oldest_plan_cached": pd.Timestamp(now - timedelta(hours=30)),
             "sqlserver_start_time": pd.Timestamp(now - timedelta(days=UPTIME_DAYS.get(server, 1))),
             "server_time": pd.Timestamp(now)}
    return QueryPerfSnapshot(connected=True, collected_at_local=now, metric=metric,
                             queries=prepare_query_stats(pd.DataFrame(rows)), cache=cache)


# ---------------------------------------------------------------------------
# Module 3 - sessions and blocking
# ---------------------------------------------------------------------------
def _session(sid, login, host, program, db, status, open_tran, has_req, blocker, wait_type, wait_ms,
             resource, cpu, elapsed_ms, reads, sql, idle, command=None):
    return {"session_id": sid, "login_name": login, "host_name": host, "program_name": program,
            "database_name": db, "session_status": status, "open_transaction_count": open_tran,
            "has_request": has_req, "blocking_session_id": blocker, "wait_type": wait_type,
            "wait_ms": wait_ms, "wait_resource": resource, "cpu_ms": cpu, "elapsed_ms": elapsed_ms,
            "logical_reads": reads, "sql_text": sql, "idle_seconds": idle, "command": command,
            "request_status": "suspended" if blocker else ("running" if has_req else None)}


def sessions(server: str, settings: Settings | None = None) -> SessionsSnapshot:
    settings = settings or get_settings()
    now = _now()
    if server in UNREACHABLE:
        return SessionsSnapshot(connected=False, collected_at_local=now, error=_unreachable(server))
    rows = [
        _session(61, "APP\\svc_web", "WEB01", "OrderWebsite", "SalesDB", "sleeping", 0, 0, 0, None, 0, None,
                 None, None, None, "SELECT TOP (20) * FROM dbo.Orders WHERE CustomerID = @p1", 4),
        _session(64, "CORP\\analyst1", "PC-ANALYST1", "Microsoft SQL Server Management Studio", "master",
                 "sleeping", 0, 0, 0, None, 0, None, None, None, None, "SELECT @@VERSION", 300),
    ]
    if server == "demo-prod-01.example.local":            # a prolonged 3-level blocking chain
        rows += [
            _session(72, "CORP\\finance.user", "PC-FIN07", "Microsoft Office", "SalesDB", "sleeping", 1, 0, 0,
                     None, 0, None, None, None, None,
                     "BEGIN TRAN; UPDATE dbo.Orders SET Status = 'Hold' WHERE OrderID = 1001;", 95),
            _session(75, "APP\\svc_api", "API02", "OrderService", "SalesDB", "suspended", 1, 1, 72, "LCK_M_U",
                     95_000, "KEY: 7:72057594045988864 (8194443284a0)", 12, 95_000, 6,
                     "UPDATE dbo.Orders SET Status = 'Shipped' WHERE OrderID = 1001", 0, "UPDATE"),
            _session(78, "APP\\svc_web", "WEB02", "OrderWebsite", "SalesDB", "suspended", 0, 1, 75, "LCK_M_S",
                     61_000, "KEY: 7:72057594045988864 (8194443284a0)", 3, 61_000, 2,
                     "SELECT Status FROM dbo.Orders WHERE OrderID = 1001", 0, "SELECT"),
        ]
    if server == "demo-report-01.example.local":          # long-running report, not blocked
        rows.append(_session(88, "APP\\svc_reports", "RPT01", "Report Server", "ReportServer", "running", 0, 1,
                             0, "CXPACKET", 2_000, None, 410_000, 245_000, 9_800_000,
                             "SELECT r.Region, SUM(s.Amount) FROM dbo.SalesHistory AS s JOIN dbo.Regions AS r "
                             "ON r.RegionID = s.RegionID GROUP BY r.Region", 0, "SELECT"))
    df = prepare_sessions(pd.DataFrame(rows), settings.app_name)
    return SessionsSnapshot(connected=True, collected_at_local=now, sessions=df, chains=blocking_chains(df))


# ---------------------------------------------------------------------------
# Module 4 - backups
# ---------------------------------------------------------------------------
# minutes since last full / diff / log per database (None = never)
BACKUP_AGES: dict[str, dict[str, tuple]] = {
    "demo-prod-01.example.local": {"*": (2 * 1440, 6 * 60, 12)},
    "demo-prod-02.example.local\\FINANCE": {"*": (3 * 1440, 9 * 60, 15), "FinanceDB": (3 * 1440, 9 * 60, 190)},
    "demo-uat-01.example.local": {"*": (6 * 1440, 20 * 60, 40), "NewFeatureDB": (None, None, None)},
    "demo-report-01.example.local": {"*": (1440, 5 * 60, 25)},
}


def backups(server: str) -> BackupSnapshot:
    now = _now()
    if server in UNREACHABLE:
        return BackupSnapshot(connected=False, collected_at_local=now, error=_unreachable(server))
    ages = BACKUP_AGES.get(server, {"*": (1440, None, 30)})
    rows, history = [], []
    for i, (name, model, data, log, _used, state) in enumerate(
            [d for d in SYSTEM_DBS if d[0] != "tempdb"] + DATABASES.get(server, []), start=1):
        full, diff, logb = ages.get(name, ages["*"])
        logb = logb if model == "FULL" else None
        ts = lambda m: pd.Timestamp(now - timedelta(minutes=m)) if m is not None else None  # noqa: E731
        rows.append({
            "database_id": i, "database_name": name, "state_desc": state, "recovery_model_desc": model,
            "log_reuse_wait_desc": "LOG_BACKUP" if (logb or 0) > 120 else "NOTHING", "is_read_only": 0,
            "is_system": 1 if name in ("master", "model", "msdb") else 0,
            "create_date": pd.Timestamp(now - timedelta(days=400)),
            "last_full": ts(full), "last_full_copy_only": 0 if full is not None else None,
            "last_full_size_mb": round(data * 0.31, 1) if full is not None else None,
            "last_diff": ts(diff), "last_log": ts(logb),
            "full_age_minutes": full, "diff_age_minutes": diff, "log_age_minutes": logb,
            "db_age_minutes": 400 * 1440, "server_time": pd.Timestamp(now),
        })
        for kind, age, size in (("Full", full, data * 0.31), ("Differential", diff, data * 0.04),
                                ("Log", logb, log * 0.02)):
            if age is not None:
                ext = "trn" if kind == "Log" else "bak"
                history.append({
                    "database_name": name, "backup_type": kind,
                    "backup_start_date": pd.Timestamp(now - timedelta(minutes=age, seconds=40)),
                    "backup_finish_date": pd.Timestamp(now - timedelta(minutes=age)),
                    "duration_seconds": 40, "age_minutes": age, "size_mb": round(size * 3, 1),
                    "compressed_mb": round(size, 1), "is_copy_only": 0, "has_checksum": 1,
                    "recovery_model": model, "user_name": "DEMO\\svc_sqlagent",
                    "physical_device_name": f"\\\\backupshare.example.local\\sql\\{name}_{kind.upper()}.{ext}",
                })
    hist = pd.DataFrame(history).sort_values("backup_finish_date", ascending=False) if history else pd.DataFrame()
    return BackupSnapshot(connected=True, collected_at_local=now, databases=pd.DataFrame(rows), history=hist)


# ---------------------------------------------------------------------------
# Module 5 - extra input for the memory-pressure rule
# ---------------------------------------------------------------------------
def memory(server: str):
    from monitor.alerts import MemoryInfo
    if server == "demo-prod-02.example.local\\FINANCE":
        return MemoryInfo(low_memory_events=3, minutes_since_last=4, sql_memory_mb=24_100, os_available_mb=410)
    return MemoryInfo()
