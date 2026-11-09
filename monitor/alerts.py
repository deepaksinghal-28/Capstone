"""
Module 5 - Rule-based alerts and DBA recommendations.

DBA analogy: like SQL Agent alerts + a runbook. Each RULE looks at the data the
other modules already collect and raises an alert when a threshold is crossed.
Every alert carries the DBA investigation steps for that situation.

No duplicates on refresh: every alert has a stable KEY (rule + target, e.g.
"backup_missing|master"). The AlertStore (a small local SQLite file) keeps one
row per key: a refresh updates "last seen" instead of inserting a new alert.
When a condition clears, the alert moves to RESOLVED. If it comes back later, it
is re-opened as a new incident.

No machine learning - plain, explainable rules (see RULES below).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from monitor import queries
from monitor.collectors import (BackupPolicy, BackupSnapshot, HealthSnapshot, SessionsSnapshot,
                                evaluate_backups, format_age_minutes)
from monitor.config import Settings
from monitor.db import DatabaseError, run_query

SEVERITY_ORDER = {"Critical": 0, "Warning": 1, "Info": 2}
SEVERITY_LABEL = {"Critical": "⛔ Critical", "Warning": "⚠️ Warning", "Info": "ℹ️ Info"}


@dataclass
class AlertCandidate:
    """An alert produced by a rule during one evaluation (before de-duplication)."""
    rule: str
    name: str
    severity: str
    target: str
    description: str
    recommendation: list[str] = field(default_factory=list)
    server: str = ""                 # friendly server name, e.g. "Primary"

    @property
    def key(self) -> str:
        """Stable identity: the same problem on the same server = the same alert."""
        base = f"{self.rule}|{self.target}"
        return f"{self.server}|{base}" if self.server else base


@dataclass
class MemoryInfo:
    """Result of the MEMORY_PRESSURE query (None when it could not be read)."""
    low_memory_events: int = 0
    minutes_since_last: float | None = None
    sql_memory_mb: float | None = None
    os_available_mb: float | None = None


def collect_memory(settings: Settings) -> tuple[MemoryInfo | None, str | None]:
    """Read low-memory notifications. Returns (info, None) or (None, friendly error)."""
    try:
        df = run_query(queries.MEMORY_PRESSURE, [settings.memory_window_minutes], settings)
    except DatabaseError as exc:
        return None, exc.friendly_message
    if df.empty:
        return MemoryInfo(), None
    r = df.iloc[0]
    num = lambda v: None if v is None or pd.isna(v) else float(v)   # noqa: E731
    return MemoryInfo(int(r["low_memory_events"] or 0), num(r["minutes_since_last"]),
                      num(r["sql_memory_mb"]), num(r["os_available_mb"])), None


# ---------------------------------------------------------------------------
# Runbook text - the recommended DBA investigation steps per rule
# ---------------------------------------------------------------------------
RUNBOOK: dict[str, list[str]] = {
    "connection_failure": [
        "Check the service: Services (services.msc) → SQL Server (MSSQLSERVER) must be Running.",
        "Read the SQL Server error log (…\\MSSQL\\Log\\ERRORLOG) and the Windows Application log for the stop reason.",
        "Try to connect with SSMS using the same server name and Windows Authentication.",
        "Check the .env settings (SQL_SERVER, SQL_ODBC_DRIVER) and that the login still exists.",
    ],
    "database_not_online": [
        "SELECT name, state_desc FROM sys.databases; - confirm the state (OFFLINE, SUSPECT, RECOVERY_PENDING, RESTORING…).",
        "Read the SQL Server error log around the time the state changed.",
        "OFFLINE by choice → ALTER DATABASE … SET ONLINE (after confirming with the owner).",
        "SUSPECT / RECOVERY_PENDING → check disk space and file paths; run DBCC CHECKDB; restore from backup if needed.",
    ],
    "database_access_restricted": [
        "Find out who set SINGLE_USER / RESTRICTED_USER and why (maintenance in progress?).",
        "When the work is finished: ALTER DATABASE … SET MULTI_USER;",
    ],
    "log_space": [
        "SELECT name, log_reuse_wait_desc FROM sys.databases; - find what prevents log reuse.",
        "LOG_BACKUP → take a transaction-log backup now; ACTIVE_TRANSACTION → find the long open transaction (DBCC OPENTRAN).",
        "Check free space on the log drive and the log file's autogrowth settings.",
        "Do not shrink the log as a routine fix - remove the cause first.",
    ],
    "backup_missing": [
        "Take a FULL backup now: BACKUP DATABASE … TO DISK = '…' WITH CHECKSUM, COMPRESSION;",
        "Schedule regular backups (SQL Agent job or maintenance plan) that match the agreed RPO.",
        "FULL recovery: also schedule log backups, otherwise the log keeps growing.",
        "Verify the file with RESTORE VERIFYONLY and perform a test restore.",
    ],
    "backup_overdue": [
        "Check the backup job history (SQL Agent) for failures or a disabled schedule.",
        "Run the overdue backup manually now (full, differential or log - see the finding).",
        "Check free space at the backup destination.",
        "Afterwards confirm the new entry in msdb.dbo.backupset and verify the file.",
    ],
    "long_running": [
        "Open Sessions & blocking: check the statement, wait type, CPU and reads of this session.",
        "Look at the execution plan (sys.dm_exec_query_plan) for scans, missing indexes, spills.",
        "Ask the owner (login / host / program) whether this run time is expected.",
        "Only if it harms others and the owner agrees, a DBA may end it manually (KILL) - the work is rolled back.",
    ],
    "blocking": [
        "Open Sessions & blocking and identify the HEAD blocker of the chain.",
        "Idle head blocker with an open transaction → contact the owner to COMMIT or ROLLBACK.",
        "Check its locks: SELECT * FROM sys.dm_tran_locks WHERE request_session_id = <head>;",
        "Last resort, with the owner's agreement: KILL <head> (manual decision - this dashboard never kills sessions).",
        "Long term: shorter transactions, supporting indexes, READ_COMMITTED_SNAPSHOT.",
    ],
    "memory_pressure": [
        "Windows signalled low physical memory to SQL Server; SQL Server trimmed its caches (plan cache, buffer pool).",
        "Check memory use of other programs (Task Manager) and SQL Server's 'max/min server memory' settings.",
        "Watch sys.dm_os_ring_buffers (RESOURCE_MONITOR) and Page Life Expectancy for repeat events.",
        "On this PC 'min server memory' = 512 MB was set to keep plan statistics stable.",
    ],
    "collection_failure": [
        "Open the affected page to see the exact error message.",
        "Check permissions: VIEW SERVER STATE / VIEW SERVER PERFORMANCE STATE, and read access to msdb.",
        "Check for query timeouts (SQL_QUERY_TIMEOUT in .env) on a very busy server.",
    ],
}

# Human-readable rule catalogue for the Alerts page.
RULES = [
    ("connection_failure", "SQL Server connection failure", "Critical", "Cannot connect to the instance"),
    ("database_not_online", "Database offline or unavailable", "Critical", "state_desc is not ONLINE"),
    ("database_access_restricted", "Database access restricted", "Warning", "SINGLE_USER or RESTRICTED_USER"),
    ("log_space", "Transaction log nearly full", "Warning / Critical",
     "Percent Log Used ≥ ALERT_LOG_USED_PCT / ALERT_LOG_USED_CRITICAL_PCT"),
    ("backup_missing", "No recorded full backup", "Critical", "No full backup in msdb for the current database"),
    ("backup_overdue", "Backup overdue", "Warning / Critical", "Backup policy of Module 4 not met"),
    ("long_running", "Long-running request", "Warning", "Running longer than ALERT_LONG_RUNNING_SECONDS"),
    ("blocking", "Blocking / prolonged blocking", "Warning / Critical",
     "Any blocked session; Critical when waiting longer than ALERT_BLOCKING_SECONDS"),
    ("memory_pressure", "Memory pressure", "Warning", "Low-memory notifications in the last ALERT_MEMORY_WINDOW_MINUTES"),
    ("collection_failure", "Monitoring data collection failure", "Warning", "A module could not read its DMVs"),
]


# ---------------------------------------------------------------------------
# Rules - each returns a list of AlertCandidate
# ---------------------------------------------------------------------------
def rule_connection(health: HealthSnapshot, settings: Settings) -> list[AlertCandidate]:
    if health.connected:
        return []
    return [AlertCandidate("connection_failure", "SQL Server connection failure", "Critical",
                           settings.server, health.error or "Cannot connect to SQL Server.",
                           RUNBOOK["connection_failure"])]


def rule_databases(health: HealthSnapshot, settings: Settings) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    dbs = health.databases
    if not health.connected or dbs.empty:
        return out
    for r in dbs.itertuples():
        if r.state_desc != "ONLINE":
            out.append(AlertCandidate("database_not_online", "Database offline or unavailable", "Critical",
                                      r.database_name, f"Database state is {r.state_desc}.",
                                      RUNBOOK["database_not_online"]))
            continue
        if r.user_access_desc != "MULTI_USER":
            out.append(AlertCandidate("database_access_restricted", "Database access restricted", "Warning",
                                      r.database_name, f"User access is {r.user_access_desc}.",
                                      RUNBOOK["database_access_restricted"]))
        pct = r.log_used_pct
        if pct is not None and pd.notna(pct) and pct >= settings.log_used_warning_pct:
            sev = "Critical" if pct >= settings.log_used_critical_pct else "Warning"
            out.append(AlertCandidate("log_space", "Transaction log nearly full", sev, r.database_name,
                                      f"{pct:.0f}% of the transaction log is in use "
                                      f"(warning {settings.log_used_warning_pct}%, critical "
                                      f"{settings.log_used_critical_pct}%).", RUNBOOK["log_space"]))
    return out


def rule_backups(backups: BackupSnapshot, policy: BackupPolicy) -> list[AlertCandidate]:
    if not backups.connected or backups.databases.empty:
        return []
    out: list[AlertCandidate] = []
    for r in evaluate_backups(backups.databases, policy).itertuples():
        if r.backup_status not in ("Critical", "Warning"):
            continue
        if pd.isna(r.full_age_minutes):
            out.append(AlertCandidate("backup_missing", "No recorded full backup", "Critical",
                                      r.database_name, r.findings, RUNBOOK["backup_missing"]))
        else:
            out.append(AlertCandidate("backup_overdue", "Backup overdue", r.backup_status,
                                      r.database_name, r.findings, RUNBOOK["backup_overdue"]))
    return out


def rule_sessions(sessions: SessionsSnapshot, settings: Settings) -> list[AlertCandidate]:
    if not sessions.connected or sessions.sessions.empty:
        return []
    df = sessions.sessions
    df = df[~df["is_monitor"]]
    out: list[AlertCandidate] = []

    # Blocking: one alert per head blocker (not per blocked session).
    if not sessions.chains.empty:
        for head, grp in sessions.chains.groupby("head_blocker"):
            longest = float(grp["wait_seconds"].max())
            sev = "Critical" if longest >= settings.blocking_warning_seconds else "Warning"
            name = "Prolonged blocking" if sev == "Critical" else "Blocking detected"
            head_row = df[df["session_id"] == head]
            who = (f"{head_row.iloc[0]['login_name']} / {head_row.iloc[0]['program_name']}"
                   if not head_row.empty else "unknown")
            out.append(AlertCandidate(
                "blocking", name, sev, f"session {int(head)}",
                f"Head blocker session {int(head)} ({who}) is blocking {len(grp)} session(s); "
                f"longest wait {longest:.0f} s (threshold {settings.blocking_warning_seconds} s). "
                f"Chain: {grp.sort_values('depth').iloc[-1]['chain']}.",
                RUNBOOK["blocking"]))

    # Long-running requests that are NOT simply blocked (blocking rule covers those).
    running = df[(df["has_request"] == 1) & (~df["is_blocked"])
                 & (df["elapsed_seconds"].fillna(0) >= settings.long_running_seconds)]
    for r in running.itertuples():
        out.append(AlertCandidate(
            "long_running", "Long-running request", "Warning", f"session {int(r.session_id)}",
            f"Session {int(r.session_id)} ({r.login_name} / {r.program_name}) has been running for "
            f"{r.elapsed_seconds:.0f} s (threshold {settings.long_running_seconds} s) in "
            f"{r.database_name}: {r.short_sql}", RUNBOOK["long_running"]))
    return out


def rule_memory(memory: MemoryInfo | None, settings: Settings) -> list[AlertCandidate]:
    if memory is None or not memory.low_memory_events:
        return []
    last = (f"{memory.minutes_since_last:.0f} min ago" if memory.minutes_since_last is not None
            else "recently")
    return [AlertCandidate(
        "memory_pressure", "Memory pressure", "Warning", settings.server,
        f"{memory.low_memory_events} Windows low-memory notification(s) in the last "
        f"{settings.memory_window_minutes} min (latest {last}). SQL Server uses "
        f"{memory.sql_memory_mb or 0:.0f} MB; Windows reports {memory.os_available_mb or 0:.0f} MB available.",
        RUNBOOK["memory_pressure"])]


def rule_collection(results: dict[str, str | None], health: HealthSnapshot,
                    settings: Settings) -> list[AlertCandidate]:
    """Module-level failures. Suppressed while the whole server is unreachable
    (the connection-failure alert already covers that - no duplicate noise)."""
    if not health.connected:
        return []
    return [AlertCandidate("collection_failure", "Monitoring data collection failure", "Warning",
                           module, error, RUNBOOK["collection_failure"])
            for module, error in results.items() if error]


def evaluate_rules(health: HealthSnapshot, sessions: SessionsSnapshot, backups: BackupSnapshot,
                   memory: MemoryInfo | None, settings: Settings,
                   memory_error: str | None = None, server_name: str = "") -> list[AlertCandidate]:
    """Run every rule once for ONE server and return its current alert candidates.
    server_name (e.g. "Primary") is stamped on every alert so servers never collide."""
    candidates = rule_connection(health, settings)
    candidates += rule_databases(health, settings)
    candidates += rule_backups(backups, BackupPolicy.from_settings(settings))
    candidates += rule_sessions(sessions, settings)
    candidates += rule_memory(memory, settings)
    candidates += rule_collection({
        "Sessions & blocking": None if sessions.connected else sessions.error,
        "Backups": None if backups.connected else backups.error,
        "Memory check": memory_error,
    }, health, settings)
    for c in candidates:
        c.server = server_name
    # One alert per key, keep the most severe if a rule produced the same key twice.
    unique: dict[str, AlertCandidate] = {}
    for c in candidates:
        if c.key not in unique or SEVERITY_ORDER[c.severity] < SEVERITY_ORDER[unique[c.key].severity]:
            unique[c.key] = c
    return list(unique.values())


# ---------------------------------------------------------------------------
# Alert store - de-duplication and history (local SQLite file)
# ---------------------------------------------------------------------------
def default_store_path(settings: Settings) -> Path:
    """%USERPROFILE%\\CapstoneMonitor\\alerts.db unless ALERT_STORE_PATH is set.

    Deliberately NOT under AppData: Windows redirects AppData for programs started
    from packaged (MSIX) apps, so the dashboard would silently get a different
    alert history depending on how it was launched. The user-profile root is not
    redirected and is outside OneDrive.
    """
    if settings.alert_store_path:
        return Path(settings.alert_store_path)
    if settings.demo_mode:            # sample alerts never mix with the real alert history
        import tempfile
        return Path(tempfile.gettempdir()) / "CapstoneMonitor-demo" / "alerts_demo.db"
    return Path.home() / "CapstoneMonitor" / "alerts.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    alert_key      TEXT PRIMARY KEY,
    server         TEXT,                     -- friendly server name (multi-instance)
    rule           TEXT NOT NULL,
    name           TEXT NOT NULL,
    severity       TEXT NOT NULL,
    target         TEXT NOT NULL,
    description    TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    status         TEXT NOT NULL,            -- ACTIVE or RESOLVED
    first_seen     TEXT NOT NULL,            -- start of the current incident
    last_seen      TEXT NOT NULL,
    resolved_at    TEXT,
    checks_seen    INTEGER NOT NULL DEFAULT 1,
    times_opened   INTEGER NOT NULL DEFAULT 1
);
"""


class AlertStore:
    """One row per alert key. Timestamps are this PC's local time (ISO format)."""

    def __init__(self, path: Path, legacy_server: str = "") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn, legacy_server)

    @staticmethod
    def _migrate(conn: sqlite3.Connection, legacy_server: str) -> None:
        """Upgrade a single-server store: add the server column and prefix old keys
        with the first server's name, so existing alerts continue (no duplicates)."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)")}
        if "server" not in columns:
            conn.execute("ALTER TABLE alerts ADD COLUMN server TEXT")
        if legacy_server:
            conn.execute("UPDATE alerts SET server = ?, alert_key = ? || '|' || alert_key "
                         "WHERE server IS NULL", (legacy_server, legacy_server))

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Open, commit on success (rollback on error) and always close."""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:              # transaction: commit / rollback
                yield conn
        finally:
            conn.close()

    def sync(self, candidates: list[AlertCandidate], now: datetime | None = None,
             history_days: int = 7) -> dict[str, int]:
        """Apply one evaluation: open new alerts, refresh existing ones, resolve cleared ones."""
        now = now or datetime.now()
        ts = now.isoformat(timespec="seconds")
        counts = {"opened": 0, "updated": 0, "reopened": 0, "resolved": 0, "purged": 0}
        current = {c.key: c for c in candidates}
        with self._conn() as conn:
            existing = {r["alert_key"]: r for r in conn.execute("SELECT * FROM alerts")}
            for key, c in current.items():
                rec = "\n".join(c.recommendation)
                row = existing.get(key)
                if row is None:
                    conn.execute(
                        "INSERT INTO alerts (alert_key, server, rule, name, severity, target, description, "
                        "recommendation, status, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
                        (key, c.server or None, c.rule, c.name, c.severity, c.target, c.description,
                         rec, ts, ts))
                    counts["opened"] += 1
                elif row["status"] == "ACTIVE":
                    conn.execute(
                        "UPDATE alerts SET name=?, severity=?, description=?, recommendation=?, "
                        "last_seen=?, checks_seen=checks_seen+1 WHERE alert_key=?",
                        (c.name, c.severity, c.description, rec, ts, key))
                    counts["updated"] += 1
                else:   # condition came back after being resolved -> new incident
                    conn.execute(
                        "UPDATE alerts SET name=?, severity=?, description=?, recommendation=?, "
                        "status='ACTIVE', first_seen=?, last_seen=?, resolved_at=NULL, checks_seen=1, "
                        "times_opened=times_opened+1 WHERE alert_key=?",
                        (c.name, c.severity, c.description, rec, ts, ts, key))
                    counts["reopened"] += 1
            for key, row in existing.items():
                if key not in current and row["status"] == "ACTIVE":
                    conn.execute("UPDATE alerts SET status='RESOLVED', resolved_at=? WHERE alert_key=?",
                                 (ts, key))
                    counts["resolved"] += 1
            cutoff = (now - timedelta(days=history_days)).isoformat(timespec="seconds")
            counts["purged"] = conn.execute(
                "DELETE FROM alerts WHERE status='RESOLVED' AND resolved_at < ?", (cutoff,)).rowcount
        return counts

    def frame(self, status: str | None = None) -> pd.DataFrame:
        sql = "SELECT * FROM alerts" + (" WHERE status = ?" if status else "")
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(sql, (status,) if status else ())]
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        for col in ("first_seen", "last_seen", "resolved_at"):
            df[col] = pd.to_datetime(df[col], errors="coerce")
        df["severity_rank"] = df["severity"].map(SEVERITY_ORDER)
        df["severity_label"] = df["severity"].map(SEVERITY_LABEL)
        return df.sort_values(["severity_rank", "first_seen"])


def summarize_alerts(active: pd.DataFrame) -> dict[str, Any]:
    if active.empty:
        return {"active": 0, "critical": 0, "warning": 0, "info": 0}
    return {
        "active": int(len(active)),
        "critical": int((active["severity"] == "Critical").sum()),
        "warning": int((active["severity"] == "Warning").sum()),
        "info": int((active["severity"] == "Info").sum()),
    }


def age_text(ts: Any, now: datetime | None = None) -> str:
    if ts is None or pd.isna(ts):
        return "–"
    minutes = ((now or datetime.now()) - pd.Timestamp(ts).to_pydatetime()).total_seconds() / 60
    return format_age_minutes(max(minutes, 0)) + " ago"
