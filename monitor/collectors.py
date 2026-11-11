"""
Collectors: run the monitoring queries and shape the results for the dashboard.

DBA analogy: a collector is like a SQL Agent job step that runs a DMV query
and stores the result - except here the result goes straight to the screen.

A collector NEVER raises an exception to the dashboard. If SQL Server is down,
it returns a snapshot with connected=False and a friendly error message, so the
page can show a clear "SQL Server unavailable" state instead of crashing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from monitor import queries
from monitor.config import Settings, get_settings
from monitor.db import DatabaseError, connect, query_df

NUMERIC_DB_COLUMNS = ["data_size_mb", "log_size_mb", "total_size_mb", "log_used_pct"]


@dataclass
class HealthSnapshot:
    """Everything Module 1 shows, collected in one round trip."""
    connected: bool
    collected_at_local: datetime                 # this PC's clock (user session)
    server: dict[str, Any] = field(default_factory=dict)
    databases: pd.DataFrame = field(default_factory=pd.DataFrame)
    error: str | None = None
    error_detail: str | None = None

    @property
    def collected_at_server(self) -> datetime | None:
        """SQL Server's own clock at collection time (used for all age maths)."""
        return self.server.get("server_time")


def format_uptime(seconds: int | float | None) -> str:
    """366 -> '6m', 93784 -> '1d 2h 3m'."""
    if seconds is None or pd.isna(seconds) or seconds < 0:
        return "n/a"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else f"{seconds}s"


def format_size_mb(mb: float | None) -> str:
    """1536 -> '1.5 GB', 40.7 -> '40.7 MB'."""
    if mb is None or pd.isna(mb):
        return "n/a"
    if mb >= 1024 * 1024:
        return f"{mb / 1024 / 1024:.2f} TB"
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.1f} MB"


def version_label(server: dict[str, Any]) -> str:
    """'SQL Server 2022 CU27' style label from SERVERPROPERTY values."""
    major = str(server.get("product_version", "")).split(".")[0]
    names = {"17": "2025", "16": "2022", "15": "2019", "14": "2017", "13": "2016"}
    year = names.get(major, f"v{major}" if major else "")
    level = server.get("product_update_level") or server.get("product_level") or ""
    return f"SQL Server {year} {level}".strip()


def prepare_databases(df: pd.DataFrame, log_warning_pct: int) -> pd.DataFrame:
    """Convert types and add a health status per database."""
    if df.empty:
        return df
    df = df.copy()
    for col in NUMERIC_DB_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    def status(row: pd.Series) -> str:
        if row["state_desc"] != "ONLINE":
            return "Critical"
        if pd.notna(row.get("log_used_pct")) and row["log_used_pct"] >= log_warning_pct:
            return "Warning"
        if row.get("user_access_desc") != "MULTI_USER":
            return "Warning"
        return "Healthy"

    df["health_status"] = df.apply(status, axis=1)
    return df


def summarize_databases(df: pd.DataFrame) -> dict[str, Any]:
    """Counts and totals for the summary cards."""
    if df.empty:
        return {"total": 0, "online": 0, "not_online": 0, "user": 0,
                "system": 0, "total_size_mb": 0.0, "warnings": 0}
    online = int((df["state_desc"] == "ONLINE").sum())
    statuses = df["health_status"] if "health_status" in df.columns else pd.Series(dtype=str)
    return {
        "total": int(len(df)),
        "online": online,
        "not_online": int(len(df) - online),
        "user": int((df["is_system"] == 0).sum()),
        "system": int((df["is_system"] == 1).sum()),
        "total_size_mb": float(df["total_size_mb"].fillna(0).sum()),
        "warnings": int((statuses == "Warning").sum()),
    }


# ---------------------------------------------------------------------------
# Module 2 - Query performance
# ---------------------------------------------------------------------------

# key -> (label shown in the UI, DataFrame column used for ranking)
QUERY_METRICS: dict[str, tuple[str, str]] = {
    "cpu":          ("Total CPU time (ms)", "total_cpu_ms"),
    "duration":     ("Total duration (ms)", "total_duration_ms"),
    "reads":        ("Total logical reads (8 KB pages)", "total_logical_reads"),
    "executions":   ("Executions", "execution_count"),
    "avg_cpu":      ("Average CPU per execution (ms)", "avg_cpu_ms"),
    "avg_duration": ("Average duration per execution (ms)", "avg_duration_ms"),
}


@dataclass
class QueryPerfSnapshot:
    """Top queries from the plan cache plus how much history the cache holds."""
    connected: bool
    collected_at_local: datetime
    metric: str = "cpu"
    queries: pd.DataFrame = field(default_factory=pd.DataFrame)
    cache: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_detail: str | None = None


def prepare_query_stats(df: pd.DataFrame) -> pd.DataFrame:
    """Convert DMV microseconds to milliseconds and add per-execution averages."""
    if df.empty:
        return df
    df = df.copy()
    executions = df["execution_count"].where(df["execution_count"] > 0)   # avoid /0
    df["total_cpu_ms"] = df["total_cpu_us"] / 1000.0
    df["total_duration_ms"] = df["total_elapsed_us"] / 1000.0
    df["max_duration_ms"] = df["max_elapsed_us"] / 1000.0
    df["avg_cpu_ms"] = df["total_cpu_ms"] / executions
    df["avg_duration_ms"] = df["total_duration_ms"] / executions
    df["avg_logical_reads"] = df["total_logical_reads"] / executions
    df["database_name"] = df["database_name"].fillna("(system)")
    df["object_name"] = df["object_name"].fillna("")
    df["query_text"] = df["query_text"].fillna("")
    df["short_text"] = (df["query_text"].str.replace(r"\s+", " ", regex=True)
                        .str.strip().str.slice(0, 90))
    return df


def rank_queries(df: pd.DataFrame, metric: str, top_n: int,
                 hide_monitor: bool = True, database: str | None = None,
                 search: str = "") -> pd.DataFrame:
    """Filter, sort by the chosen metric and number the rows 1..N."""
    if df.empty:
        return df
    if metric not in QUERY_METRICS:
        raise ValueError(f"Unknown metric {metric!r}")
    column = QUERY_METRICS[metric][1]
    out = df
    if hide_monitor and "is_monitor_query" in out:
        out = out[out["is_monitor_query"] == 0]
    if database:
        out = out[out["database_name"] == database]
    if search:
        out = out[out["query_text"].str.contains(search, case=False, regex=False)
                  | out["object_name"].str.contains(search, case=False, regex=False)]
    out = out.sort_values(column, ascending=False).head(top_n).copy()
    out.insert(0, "rank", range(1, len(out) + 1))
    return out


ALL_DATABASES = "All databases"


def database_filter_options(queries: pd.DataFrame, databases: pd.DataFrame | None = None) -> list[str]:
    """Choices for the Query Performance "Database" filter: every database on the server (the
    Module 1 list) plus any database seen in the plan cache. A database with no cached queries can
    still be chosen - the page then says why it shows nothing, instead of leaving it out."""
    names: set[str] = set()
    for frame in (databases, queries):
        if frame is not None and not frame.empty and "database_name" in frame:
            names.update(frame["database_name"].dropna().astype(str))
    return [ALL_DATABASES] + sorted(names, key=str.lower)


def collect_query_stats(metric: str = "cpu", fetch_rows: int = 100,
                        settings: Settings | None = None) -> QueryPerfSnapshot:
    """Collect top query shapes from the plan cache. Never raises."""
    if metric not in QUERY_METRICS:
        raise ValueError(f"Unknown metric {metric!r}")
    settings = settings or get_settings()
    now_local = datetime.now()
    try:
        with connect(settings) as conn:
            top_df = query_df(conn, queries.TOP_QUERIES, [int(fetch_rows), metric], settings)
            cache_df = query_df(conn, queries.PLAN_CACHE_SUMMARY, settings=settings)
    except DatabaseError as exc:
        return QueryPerfSnapshot(connected=False, collected_at_local=now_local, metric=metric,
                                 error=exc.friendly_message, error_detail=exc.technical_detail)
    return QueryPerfSnapshot(
        connected=True,
        collected_at_local=now_local,
        metric=metric,
        queries=prepare_query_stats(top_df),
        cache=cache_df.iloc[0].to_dict() if not cache_df.empty else {},
    )


# ---------------------------------------------------------------------------
# Module 3 - Active sessions and blocking
# ---------------------------------------------------------------------------

SESSION_STATE_LABEL = {
    "head_blocker": "⛔ Head blocker",
    "blocked_blocker": "🔒 Blocked + blocking",
    "blocked": "🔒 Blocked",
    "running": "▶️ Running",
    "idle_open_tran": "⚠️ Idle, open transaction",
    "sleeping": "💤 Sleeping",
}


@dataclass
class SessionsSnapshot:
    """A live picture of user sessions at one moment."""
    connected: bool
    collected_at_local: datetime
    sessions: pd.DataFrame = field(default_factory=pd.DataFrame)
    chains: pd.DataFrame = field(default_factory=pd.DataFrame)
    requests_skipped: bool = False          # True when the CU27 recovery guard was triggered
    recovering_databases: list[str] = field(default_factory=list)
    error: str | None = None
    error_detail: str | None = None


SESSION_DERIVED_COLUMNS = {
    "is_blocked": bool, "is_blocker": bool, "is_head_blocker": bool, "is_monitor": bool,
    "wait_seconds": float, "elapsed_seconds": float, "short_sql": object, "state": object,
    "state_label": object,
}


def prepare_sessions(df: pd.DataFrame, app_name: str = "") -> pd.DataFrame:
    """Add blocked / blocker / head-blocker flags and a readable state per session."""
    if df.empty:
        # No user sessions (e.g. an idle instance): still return every column the
        # pages use, so filters like df[~df["is_monitor"]] work on an empty table.
        df = df.copy()
        for col, dtype in SESSION_DERIVED_COLUMNS.items():
            if col not in df.columns:
                df[col] = pd.Series(dtype=dtype)
        return df
    df = df.drop_duplicates(subset="session_id").copy()
    for col in ("blocking_session_id", "wait_ms", "cpu_ms", "elapsed_ms", "logical_reads",
                "open_transaction_count", "idle_seconds", "has_request"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["blocking_session_id"] = df["blocking_session_id"].fillna(0).astype(int)
    # Negative IDs (-2 orphaned DTC, -3 deferred recovery, -4 latch) are not real sessions.
    df["is_blocked"] = df["blocking_session_id"] > 0
    blockers = set(df.loc[df["is_blocked"], "blocking_session_id"])
    df["is_blocker"] = df["session_id"].isin(blockers)
    df["is_head_blocker"] = df["is_blocker"] & ~df["is_blocked"]
    df["is_monitor"] = df["program_name"].fillna("") == app_name if app_name else False
    df["wait_seconds"] = df["wait_ms"].fillna(0) / 1000.0
    df["elapsed_seconds"] = df["elapsed_ms"] / 1000.0
    df["open_transaction_count"] = df["open_transaction_count"].fillna(0).astype(int)
    df["sql_text"] = df["sql_text"].fillna("")
    df["short_sql"] = df["sql_text"].str.replace(r"\s+", " ", regex=True).str.strip().str.slice(0, 90)

    def state(row: pd.Series) -> str:
        if row["is_head_blocker"]:
            return "head_blocker"
        if row["is_blocked"] and row["is_blocker"]:
            return "blocked_blocker"
        if row["is_blocked"]:
            return "blocked"
        if row.get("has_request") == 1:
            return "running"
        if row["open_transaction_count"] > 0:
            return "idle_open_tran"
        return "sleeping"

    df["state"] = df.apply(state, axis=1)
    df["state_label"] = df["state"].map(SESSION_STATE_LABEL)
    return df


def blocking_chains(df: pd.DataFrame) -> pd.DataFrame:
    """
    One row per BLOCKED session, with the full chain back to its head blocker,
    e.g. chain '61 → 62 → 63' means 61 blocks 62, which blocks 63.
    """
    columns = ["session_id", "blocked_by", "head_blocker", "chain", "depth", "wait_type",
               "wait_seconds", "wait_resource", "database_name", "login_name",
               "program_name", "sql_text"]
    if df.empty or not df["is_blocked"].any():
        return pd.DataFrame(columns=columns)
    parent = dict(zip(df["session_id"], df["blocking_session_id"]))
    rows = []
    for row in df[df["is_blocked"]].itertuples():
        path, current, seen = [row.session_id], row.session_id, {row.session_id}
        while parent.get(current, 0) > 0:
            current = parent[current]
            if current in seen:                      # cycle (deadlock in progress)
                path.append(current)
                break
            seen.add(current)
            path.append(current)
        head = path[-1]
        rows.append({
            "session_id": row.session_id,
            "blocked_by": row.blocking_session_id,
            "head_blocker": head,
            "chain": " → ".join(str(s) for s in reversed(path)),
            "depth": len(path) - 1,
            "wait_type": row.wait_type,
            "wait_seconds": row.wait_seconds,
            "wait_resource": row.wait_resource,
            "database_name": row.database_name,
            "login_name": row.login_name,
            "program_name": row.program_name,
            "sql_text": row.sql_text,
        })
    return pd.DataFrame(rows, columns=columns).sort_values(["head_blocker", "depth"])


def summarize_sessions(df: pd.DataFrame) -> dict[str, Any]:
    """Counts for the summary cards (the monitor's own sessions excluded)."""
    if df.empty:
        return {"sessions": 0, "running": 0, "blocked": 0, "head_blockers": 0,
                "longest_wait_s": 0.0, "longest_request_s": 0.0, "open_tran": 0}
    d = df[~df["is_monitor"]] if "is_monitor" in df else df
    running = d[d["has_request"] == 1]
    return {
        "sessions": int(len(d)),
        "running": int(len(running)),
        "blocked": int(d["is_blocked"].sum()),
        "head_blockers": int(d["is_head_blocker"].sum()),
        "longest_wait_s": float(d.loc[d["is_blocked"], "wait_seconds"].max())
        if d["is_blocked"].any() else 0.0,
        "longest_request_s": float(running["elapsed_seconds"].max())
        if not running.empty and running["elapsed_seconds"].notna().any() else 0.0,
        "open_tran": int((d["open_transaction_count"] > 0).sum()),
    }


def collect_sessions(settings: Settings | None = None) -> SessionsSnapshot:
    """Collect user sessions and blocking. Never raises."""
    settings = settings or get_settings()
    now_local = datetime.now()
    try:
        with connect(settings) as conn:
            recovering = query_df(conn, queries.DATABASES_IN_RECOVERY, settings=settings)
            include_requests = recovering.empty
            raw = query_df(conn, queries.sessions_query(include_requests), settings=settings)
    except DatabaseError as exc:
        return SessionsSnapshot(connected=False, collected_at_local=now_local,
                                error=exc.friendly_message, error_detail=exc.technical_detail)
    sessions = prepare_sessions(raw, settings.app_name)
    return SessionsSnapshot(
        connected=True,
        collected_at_local=now_local,
        sessions=sessions,
        chains=blocking_chains(sessions) if not sessions.empty else blocking_chains(pd.DataFrame()),
        requests_skipped=not include_requests,
        recovering_databases=recovering["name"].tolist() if not recovering.empty else [],
    )


# ---------------------------------------------------------------------------
# Module 4 - Backup monitoring
# ---------------------------------------------------------------------------

BACKUP_STATUS_LABEL = {"Healthy": "✅ Healthy", "Warning": "⚠️ Overdue",
                       "Critical": "⛔ Critical", "Not checked": "➖ Not checked"}


@dataclass(frozen=True)
class BackupPolicy:
    full_max_hours: int = 168
    data_max_hours: int = 24
    log_max_minutes: int = 60

    @classmethod
    def from_settings(cls, s: Settings) -> "BackupPolicy":
        return cls(s.full_backup_max_hours, s.data_backup_max_hours, s.log_backup_max_minutes)


@dataclass
class BackupSnapshot:
    connected: bool
    collected_at_local: datetime
    databases: pd.DataFrame = field(default_factory=pd.DataFrame)
    history: pd.DataFrame = field(default_factory=pd.DataFrame)
    error: str | None = None
    error_detail: str | None = None


def format_age_minutes(minutes: float | None) -> str:
    """75 -> '1 h 15 min', 3000 -> '2 d 2 h', None -> 'never'."""
    if minutes is None or pd.isna(minutes):
        return "never"
    minutes = int(minutes)
    if minutes < 1:
        return "< 1 min"
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {mins} min"
    return f"{mins} min"


def evaluate_backups(df: pd.DataFrame, policy: BackupPolicy) -> pd.DataFrame:
    """Apply the backup policy to each database and explain every finding."""
    if df.empty:
        return df
    df = df.copy()
    for col in ("full_age_minutes", "diff_age_minutes", "log_age_minutes", "last_full_size_mb"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    statuses, issues, data_ages, log_states = [], [], [], []
    for r in df.itertuples():
        found: list[str] = []
        status = "Healthy"
        full_age, diff_age, log_age = r.full_age_minutes, r.diff_age_minutes, r.log_age_minutes
        # A differential only counts if it is newer than the last full (its base).
        diff_valid = pd.notna(diff_age) and (pd.isna(full_age) or diff_age <= full_age)
        data_age = min(a for a in (full_age, diff_age if diff_valid else None)
                       if a is not None and pd.notna(a)) if pd.notna(full_age) else None
        log_model = r.recovery_model_desc in ("FULL", "BULK_LOGGED")

        if r.state_desc != "ONLINE":
            status, found = "Not checked", [f"Database is {r.state_desc}"]
            log_state = "–"
        elif pd.isna(full_age):
            status = "Critical"
            found.append("No recorded full backup")
            if log_model:
                found.append(f"{r.recovery_model_desc} recovery but no full backup yet: the log chain "
                             "has not started (behaves like SIMPLE until the first full backup)")
            log_state = "Not applicable (SIMPLE)" if not log_model else "Chain not started"
        else:
            if full_age > policy.full_max_hours * 60:
                status = "Critical"
                found.append(f"Last full backup {format_age_minutes(full_age)} ago "
                             f"(policy: every {policy.full_max_hours} h)")
            if data_age is not None and data_age > policy.data_max_hours * 60:
                status = "Critical" if status == "Critical" else "Warning"
                found.append(f"Last full/differential {format_age_minutes(data_age)} ago "
                             f"(policy: every {policy.data_max_hours} h)")
            if log_model:
                # No log backup yet: measure from the full backup that started the chain.
                since = log_age if pd.notna(log_age) else full_age
                if since > policy.log_max_minutes:
                    status = "Critical" if status == "Critical" else "Warning"
                    found.append(
                        (f"Log backup overdue: last log {format_age_minutes(log_age)} ago"
                         if pd.notna(log_age) else
                         f"No log backup since the full backup {format_age_minutes(full_age)} ago")
                        + f" (policy: every {policy.log_max_minutes} min)")
                log_state = format_age_minutes(log_age) + (" ago" if pd.notna(log_age) else "")
            else:
                log_state = "Not applicable (SIMPLE)"
            if getattr(r, "log_reuse_wait_desc", "") == "LOG_BACKUP":
                found.append("Log cannot be reused until a log backup is taken (log_reuse_wait = LOG_BACKUP)")
        statuses.append(status)
        issues.append("; ".join(found) if found else "Within policy")
        data_ages.append(data_age)
        log_states.append(log_state)

    df["backup_status"] = statuses
    df["status_label"] = df["backup_status"].map(BACKUP_STATUS_LABEL)
    df["findings"] = issues
    df["data_age_minutes"] = data_ages
    df["log_backup_state"] = log_states
    df["full_age_text"] = df["full_age_minutes"].map(format_age_minutes)
    df["diff_age_text"] = df["diff_age_minutes"].map(format_age_minutes)
    return df


def summarize_backups(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {"databases": 0, "healthy": 0, "warning": 0, "critical": 0, "newest_age": None}
    return {
        "databases": int(len(df)),
        "healthy": int((df["backup_status"] == "Healthy").sum()),
        "warning": int((df["backup_status"] == "Warning").sum()),
        "critical": int((df["backup_status"] == "Critical").sum()),
        "newest_age": df[["full_age_minutes", "diff_age_minutes", "log_age_minutes"]].min().min(),
    }


def collect_backups(settings: Settings | None = None, history_rows: int = 100) -> BackupSnapshot:
    """Collect last-backup facts per database and recent history. Never raises."""
    settings = settings or get_settings()
    now_local = datetime.now()
    try:
        with connect(settings) as conn:
            last = query_df(conn, queries.LAST_BACKUPS, settings=settings)
            history = query_df(conn, queries.BACKUP_HISTORY, [int(history_rows)], settings)
    except DatabaseError as exc:
        return BackupSnapshot(connected=False, collected_at_local=now_local,
                              error=exc.friendly_message, error_detail=exc.technical_detail)
    if not history.empty:
        for col in ("size_mb", "compressed_mb"):
            history[col] = pd.to_numeric(history[col], errors="coerce")
    return BackupSnapshot(connected=True, collected_at_local=now_local,
                          databases=last, history=history)


# ---------------------------------------------------------------------------
# CPU trend (SQL Server's own per-minute history - nothing stored by this app)
# ---------------------------------------------------------------------------
@dataclass
class CpuHistory:
    connected: bool
    points: pd.DataFrame = field(default_factory=pd.DataFrame)   # event_time, sql_cpu_pct, other_cpu_pct
    error: str | None = None

    @property
    def latest_total(self) -> float | None:
        if self.points.empty:
            return None
        last = self.points.sort_values("event_time").iloc[-1]
        return float(last["sql_cpu_pct"] + last["other_cpu_pct"])


def collect_cpu_history(settings: Settings | None = None, minutes: int = 60) -> CpuHistory:
    settings = settings or get_settings()
    try:
        with connect(settings) as conn:
            df = query_df(conn, queries.CPU_HISTORY, [int(minutes)], settings)
    except DatabaseError as exc:
        return CpuHistory(connected=False, error=exc.friendly_message)
    if not df.empty:
        for col in ("sql_cpu_pct", "other_cpu_pct"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).clip(lower=0)
        df = df.sort_values("event_time")
    return CpuHistory(connected=True, points=df)


def collect_health(settings: Settings | None = None) -> HealthSnapshot:
    """Collect Module 1 data. Never raises; failures come back as connected=False."""
    settings = settings or get_settings()
    now_local = datetime.now()
    try:
        with connect(settings) as conn:
            server_df = query_df(conn, queries.SERVER_INFO, settings=settings)
            db_df = query_df(conn, queries.DATABASES, settings=settings)
    except DatabaseError as exc:
        return HealthSnapshot(connected=False, collected_at_local=now_local,
                              error=exc.friendly_message, error_detail=exc.technical_detail)

    server = server_df.iloc[0].to_dict() if not server_df.empty else {}
    return HealthSnapshot(
        connected=True,
        collected_at_local=now_local,
        server=server,
        databases=prepare_databases(db_df, settings.log_used_warning_pct),
    )
