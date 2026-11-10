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

import math
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
# CPU trend (last 60 minutes, one point per minute)
# ---------------------------------------------------------------------------
CPU_LEVEL = {"demo-prod-01.example.local": 38, "demo-prod-02.example.local\\FINANCE": 64,
             "demo-uat-01.example.local": 12, "demo-report-01.example.local": 81}


def cpu_history(server: str, minutes: int = 60):
    from monitor.collectors import CpuHistory
    if server in UNREACHABLE:
        return CpuHistory(connected=False, error=_unreachable(server))
    rng, now, base = _rng(server, "cpu"), _now(), CPU_LEVEL.get(server, 20)
    rows = []
    for m in range(minutes, 0, -1):
        wave = 10 if (m // 12) % 2 else -6
        sql = max(1, min(95, base + wave + rng.randint(-8, 8)))
        rows.append({"event_time": pd.Timestamp(now - timedelta(minutes=m)),
                     "sql_cpu_pct": sql, "other_cpu_pct": rng.randint(2, 9)})
    return CpuHistory(connected=True, points=pd.DataFrame(rows))


# ---------------------------------------------------------------------------
# Module 5 - extra input for the memory-pressure rule
# ---------------------------------------------------------------------------
def memory(server: str):
    from monitor.alerts import MemoryInfo
    if server == "demo-prod-02.example.local\\FINANCE":
        return MemoryInfo(low_memory_events=3, minutes_since_last=4, sql_memory_mb=24_100, os_available_mb=410)
    return MemoryInfo()


# ---------------------------------------------------------------------------
# Module 6 - performance history: 14 days, one snapshot every 15 minutes.
# The LAST point of every series equals what the live pages show right now.
# ---------------------------------------------------------------------------
HISTORY_DAYS = 14
HISTORY_STEP_MINUTES = 15
DB_GROWTH_MB_PER_DAY = {"SalesDB": 85, "InventoryDB": 12, "CustomerDB": 40, "ReportingDW": 260,
                        "FinanceDB": 30, "PayrollDB": 6, "AuditDB": 22, "SalesDB_UAT": 4,
                        "NewFeatureDB": 18, "ReportServer": 3}
NIGHTLY_LOAD = {"ReportingDW"}            # grows in one step per night (01:00 ETL load)
# past blocking incidents: (days ago, hour of day, minutes, max blocked sessions, longest wait s)
BLOCKING_INCIDENTS = {
    "demo-prod-01.example.local": [(2, 10.25, 30, 5, 640), (6, 14.0, 20, 3, 210), (9, 11.5, 15, 2, 95)],
    "demo-prod-02.example.local\\FINANCE": [(4, 17.75, 15, 2, 75)],
    "demo-report-01.example.local": [(1, 2.0, 45, 4, 1200)],
}
OUTAGE_HOURS = {"demo-dev-01.example.local": 3}        # unreachable for the last N hours
OFFLINE_SINCE_DAYS = {"ArchiveOld": 5}                  # taken offline N days ago
LOG_BACKUPS_STOPPED_MINUTES = {"FinanceDB": 190}        # = its last log backup (BACKUP_AGES)
NIGHTLY_BATCH = {"demo-report-01.example.local"}        # heavy report batch 01:00-03:00


def _busy(ts: datetime) -> float:
    """0..1 - how busy a business system usually is: office hours, quieter at weekends."""
    hour = ts.hour + ts.minute / 60
    level = math.exp(-((hour - 13.0) / 3.2) ** 2)     # peak early afternoon
    return level * (0.35 if ts.weekday() >= 5 else 1.0)


# Who blocked whom in the scripted incidents where a session waited 5 minutes or more.
# head: session, login, host, program, status, open transactions, last statement
# blocked: session, login, host, program, database, wait type, wait resource,
#          minutes after the incident began, seconds waited, blocked by, statement
LONG_BLOCKS = {
    ("demo-prod-01.example.local", 2): {
        "head": (58, "CORP\\month.end", "PC-FIN03", "Microsoft Office Excel", "sleeping", 1,
                 "BEGIN TRAN;\nUPDATE dbo.Invoices SET Status = 'Closing' WHERE PeriodID = 202611;\n"
                 "-- no COMMIT: the transaction stayed open"),
        "blocked": [
            (63, "APP\\svc_billing", "APP01", "BillingService", "SalesDB", "LCK_M_U",
             "KEY: 7:72057594046119936 (61a06abd401c)", 0, 640, 58,
             "UPDATE dbo.Invoices SET Paid = 1, PaidOn = SYSDATETIME() WHERE InvoiceID = @p1"),
            (66, "APP\\svc_web", "WEB01", "OrderWebsite", "SalesDB", "LCK_M_S",
             "KEY: 7:72057594046119936 (61a06abd401c)", 4, 410, 63,
             "SELECT InvoiceID, Status, Amount FROM dbo.Invoices WHERE CustomerID = @p1"),
        ]},
    ("demo-report-01.example.local", 1): {
        "head": (91, "CORP\\svc_etl", "ETL01", "SQLAgentAlpha - TSQL JobStep (Nightly sales load)", "running", 1,
                 "INSERT INTO dbo.SalesHistory WITH (TABLOCK)\n"
                 "SELECT * FROM staging.Sales WHERE LoadDate = CAST(GETDATE() AS date);"),
        "blocked": [
            (88, "APP\\svc_reports", "RPT01", "Report Server", "ReportServer", "LCK_M_IS",
             "OBJECT: 9:245575913:0", 2, 1200, 91,
             "SELECT r.Region, SUM(s.Amount) FROM dbo.SalesHistory AS s JOIN dbo.Regions AS r "
             "ON r.RegionID = s.RegionID GROUP BY r.Region"),
        ]},
}


def _long_blocks(server: str, end: datetime, threshold_seconds: int) -> pd.DataFrame:
    """Long-blocking events (same columns as history.db) for the scripted incidents."""
    from monitor.history import BLOCKING_COLUMNS
    rows = []
    for days_ago, hour, *_ in BLOCKING_INCIDENTS.get(server, []):
        script = LONG_BLOCKS.get((server, days_ago))
        if script is None:
            continue
        start = (end - timedelta(days=days_ago)).replace(hour=int(hour), minute=int(round(hour % 1 * 60)), second=0)
        h_sid, h_login, h_host, h_program, h_state, h_tran, h_sql = script["head"]
        parent = {b[0]: b[9] for b in script["blocked"]}
        for sid, login, host, program, db, wait_type, resource, after_min, waited, by, sql in script["blocked"]:
            if waited < threshold_seconds:
                continue
            chain, current = [sid], sid
            while current in parent:
                current = parent[current]
                chain.append(current)
            since = start + timedelta(minutes=after_min)
            rows.append({
                "blocked_since": since, "first_seen": since + timedelta(seconds=threshold_seconds),
                "last_seen": since + timedelta(seconds=waited), "longest_wait_s": float(waited),
                "blocked_session": sid, "blocked_login": login, "blocked_host": host, "blocked_program": program,
                "database_name": db, "wait_type": wait_type, "wait_resource": resource, "blocked_sql": sql,
                "blocked_by": by, "head_blocker": h_sid, "head_login": h_login, "head_host": h_host,
                "head_program": h_program, "head_status": f"{h_state} with {h_tran} open transaction(s)",
                "head_open_tran": h_tran, "head_sql": h_sql, "chain": " → ".join(str(s) for s in reversed(chain))})
    return pd.DataFrame(rows, columns=BLOCKING_COLUMNS)


def _incident_at(server: str, t: datetime, end: datetime) -> tuple[int, float]:
    """(blocked sessions, longest wait s) of a scripted past blocking incident at time t."""
    for days_ago, hour, minutes, max_blocked, longest in BLOCKING_INCIDENTS.get(server, []):
        day = end - timedelta(days=days_ago)
        start = day.replace(hour=int(hour), minute=int(round(hour % 1 * 60)), second=0)
        if start <= t < start + timedelta(minutes=minutes):
            frac = (t - start) / timedelta(minutes=minutes)
            waited = min(float(longest), (t - start).total_seconds() + 60)   # a lock wait grows with time
            return max(1, round(max_blocked * math.sin(math.pi * (0.15 + 0.7 * frac)))), waited
    return 0, 0.0


def history(server: str, name: str = "", settings: Settings | None = None):
    """Generated 14-day history for one demo server (same shape as monitor.history.HistoryStore.load)."""
    from monitor.alerts import SCORE_PENALTY, evaluate_rules
    from monitor.collectors import summarize_sessions
    from monitor.history import History

    settings = settings or get_settings()
    rng = _rng(server, "history")
    end = _now().replace(second=0)
    step = timedelta(minutes=HISTORY_STEP_MINUTES)
    times = [end - step * i for i in range(HISTORY_DAYS * 24 * 60 // HISTORY_STEP_MINUTES, -1, -1)]
    outage = end - timedelta(hours=OUTAGE_HOURS[server]) if server in OUTAGE_HOURS else None

    live_health, live_sessions = health(server, settings), sessions(server, settings)
    live = summarize_sessions(live_sessions.sessions) if live_sessions.connected else {}
    dbs = SYSTEM_DBS + DATABASES.get(server, [])
    system_names = {d[0] for d in SYSTEM_DBS}

    def log_used(db_name: str, model: str, used_now, t: datetime) -> float | None:
        if used_now is None:
            return None
        if db_name in LOG_BACKUPS_STOPPED_MINUTES:          # log backups stopped -> the log only fills
            stopped = end - timedelta(minutes=LOG_BACKUPS_STOPPED_MINUTES[db_name])
            if t >= stopped:
                return 18 + (used_now - 18) * ((t - stopped) / (end - stopped))
            used_now = 18
        if model == "FULL":                                 # hourly log backups -> saw-tooth
            amp = 10 * (0.4 + _busy(t))
            value = used_now + amp * (t.minute - end.minute) / 60 + rng.uniform(-1.5, 1.5)
        else:                                               # SIMPLE: checkpoints keep it low
            value = used_now + rng.uniform(-3, 3)
        return float(min(99, max(1, value)))

    def db_state(db_name: str, state_now: str, t: datetime) -> str:
        if db_name in OFFLINE_SINCE_DAYS and t < end - timedelta(days=OFFLINE_SINCE_DAYS[db_name]):
            return "ONLINE"
        return state_now

    def modeled_alerts(blocked: int, longest: float, rows: list[dict]) -> tuple[int, int]:
        """(critical, warning) alerts this generator models: blocking, log space, offline databases."""
        crit = warn = 0
        if blocked:
            crit, warn = (1, 0) if longest >= settings.blocking_warning_seconds else (0, 1)
        for r in rows:
            if r["state_desc"] != "ONLINE":
                crit += 1
            elif r["log_used_pct"] is not None and r["log_used_pct"] >= settings.log_used_warning_pct:
                if r["log_used_pct"] >= settings.log_used_critical_pct:
                    crit += 1
                else:
                    warn += 1
        return crit, warn

    level = max(int(live.get("sessions", 0) or 0), 4)
    memory_now = float(live_health.server.get("sql_memory_in_use_mb") or 9_800)
    server_rows, db_rows = [], []
    for t in times:
        last = t == end
        connected = outage is None or t < outage
        rows = []
        for db_name, model, data, log, used, state in dbs:
            nights = ((end - timedelta(hours=1)).date() - (t - timedelta(hours=1)).date()).days
            days_ago = (end - t) / timedelta(days=1)
            growth = DB_GROWTH_MB_PER_DAY.get(db_name, 0) * (nights if db_name in NIGHTLY_LOAD else days_ago)
            state_t = db_state(db_name, state, t)
            pct = used if last else log_used(db_name, model, used, t) if state_t == "ONLINE" else None
            rows.append({"collected_at": t, "database_name": db_name, "state_desc": state_t,
                         "is_system": int(db_name in system_names), "data_mb": round(data - growth, 1),
                         "log_mb": log, "log_used_pct": pct})
        if last:
            blocked, longest = int(live.get("blocked", 0)), float(live.get("longest_wait_s", 0.0))
            users = int(live.get("sessions", 0) or 0)
        else:
            blocked, longest = _incident_at(server, t, end)
            users = max(0, round(level * (0.4 + 1.2 * _busy(t)) + rng.uniform(-1, 1)))
        crit, warn = modeled_alerts(blocked, longest, rows)
        server_rows.append({
            "collected_at": t, "connected": connected, "crit_m": crit, "warn_m": warn,
            "user_sessions": users if connected else None,
            "running_requests": (int(live.get("running", 0)) if last else round(users * 0.3)) if connected else None,
            "blocked_sessions": blocked if connected else None,
            "longest_block_s": longest if connected else None,
            "open_tran": (int(live.get("open_tran", 0)) if last else rng.randint(0, 2)) if connected else None,
            "sql_memory_mb": round(memory_now * rng.uniform(0.97, 1.0)) if connected else None,
            "databases_total": len(rows) if connected else None,
            "databases_online": sum(r["state_desc"] == "ONLINE" for r in rows) if connected else None,
        })
        if connected:
            db_rows += rows

    servers_df = pd.DataFrame(server_rows)
    # Health score: alerts the generator does not model (backups, memory ...) are held
    # constant at today's count, so the final point equals the live score on the home page.
    fixed = {"Critical": 0, "Warning": 0, "Info": 0}
    if live_health.connected:
        for c in evaluate_rules(live_health, live_sessions, backups(server), memory(server), settings,
                                server_name=name or server):
            fixed[c.severity] += 1
        fixed["Critical"] = max(0, fixed["Critical"] - int(servers_df["crit_m"].iloc[-1]))
        fixed["Warning"] = max(0, fixed["Warning"] - int(servers_df["warn_m"].iloc[-1]))
    servers_df["alerts_critical"] = servers_df["crit_m"] + fixed["Critical"]
    servers_df["alerts_warning"] = servers_df["warn_m"] + fixed["Warning"]
    servers_df["health_score"] = (100 - SCORE_PENALTY["Critical"] * servers_df["alerts_critical"]
                                  - SCORE_PENALTY["Warning"] * servers_df["alerts_warning"]
                                  - SCORE_PENALTY["Info"] * fixed["Info"]).clip(lower=0)
    servers_df.loc[~servers_df["connected"], ["health_score", "alerts_critical", "alerts_warning"]] = [0, 1, 0]
    servers_df = servers_df.drop(columns=["crit_m", "warn_m"])

    # CPU: every 5 minutes, plus the per-minute last hour the home page shows.
    base = CPU_LEVEL.get(server, 20)
    cpu_rows, t = [], end - timedelta(days=HISTORY_DAYS)
    while t < end - timedelta(minutes=60):
        if outage is None or t < outage:
            sql = base * 0.55 + 38 * _busy(t) + rng.uniform(-6, 6)
            if server in NIGHTLY_BATCH and 1 <= t.hour < 3:
                sql += 45
            cpu_rows.append({"event_time": t, "sql_cpu_pct": float(min(97, max(1, sql))),
                             "other_cpu_pct": float(rng.randint(2, 9))})
        t += timedelta(minutes=5)
    recent = cpu_history(server)
    cpu_df = pd.DataFrame(cpu_rows)
    if recent.connected:
        cpu_df = pd.concat([cpu_df, recent.points], ignore_index=True)
    cpu_df["event_time"] = pd.to_datetime(cpu_df["event_time"])

    return History(servers=servers_df, databases=pd.DataFrame(db_rows), cpu=cpu_df,
                   blocking=_long_blocks(server, end, settings.blocking_detail_seconds),
                   interval_seconds=HISTORY_STEP_MINUTES * 60, first_snapshot=times[0],
                   last_snapshot=times[-1], sample=True)
