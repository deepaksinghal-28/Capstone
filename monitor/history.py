"""
Module 6 - Performance history and trend analysis.

The live pages show each server as it is NOW. This module keeps a snapshot of
every server every few minutes, so the dashboard can also answer
"how has it changed?": CPU through the day, how fast each database grows,
how often blocking happens, and the health score over time.

    tools/collect_history.py   the COLLECTOR - started by run_app.ps1, every
                               HISTORY_INTERVAL_SECONDS (default 5 min):
        1. reads each monitored server (the same read-only DMV queries as the pages)
        2. checks the alert rules, so alerts are recorded even with no dashboard open
        3. saves one snapshot per server into history.db (SQLite on this PC)
      and every BLOCKING_CHECK_SECONDS (default 1 min) a quick sessions-only check:
        a session blocked for BLOCKING_DETAIL_SECONDS (default 5 min) or longer is saved
        with its statement, the head blocker (login, host, program, status) and the
        head blocker's last statement - so "who blocked which query" is known afterwards
    views/history.py           the PAGE - reads history.db only, never SQL Server

Nothing is ever written to the monitored SQL Servers. All times are this PC's
local clock; SQL Server's own CPU timestamps are converted to it.
Snapshots older than HISTORY_RETENTION_DAYS (default 30) are deleted.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd

from monitor.alerts import (SEVERITY_ORDER, AlertCandidate, AlertStore, collect_memory,
                            default_store_path, evaluate_rules, health_score)
from monitor.collectors import (BackupSnapshot, SessionsSnapshot, collect_backups,
                                collect_cpu_history, collect_health, collect_sessions,
                                summarize_sessions)
from monitor.config import ServerTarget, Settings, server_targets, settings_for

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
SERVER_COLUMNS = ["health_score", "alerts_critical", "alerts_warning", "user_sessions",
                  "running_requests", "blocked_sessions", "longest_block_s", "open_tran",
                  "sql_memory_mb", "databases_total", "databases_online"]
DATABASE_COLUMNS = ["database_name", "state_desc", "is_system", "data_mb", "log_mb", "log_used_pct"]
CPU_COLUMNS = ["event_time", "sql_cpu_pct", "other_cpu_pct"]
BLOCKING_COLUMNS = ["blocked_since", "first_seen", "last_seen", "longest_wait_s",
                    "blocked_session", "blocked_login", "blocked_host", "blocked_program",
                    "database_name", "wait_type", "wait_resource", "blocked_sql",
                    "blocked_by", "head_blocker", "head_login", "head_host", "head_program",
                    "head_status", "head_open_tran", "head_sql", "chain"]
SAME_WAIT_SECONDS = 120     # two sightings whose wait started within 2 min = the same wait

SCHEMA = """
CREATE TABLE IF NOT EXISTS server_snapshots (
    server            TEXT    NOT NULL,   -- friendly name, e.g. Primary
    collected_at      TEXT    NOT NULL,   -- this PC's local time (ISO)
    connected         INTEGER NOT NULL,   -- 0 = server was unreachable
    health_score      INTEGER,            -- 0-100, same formula as the home page
    alerts_critical   INTEGER,
    alerts_warning    INTEGER,
    user_sessions     INTEGER,
    running_requests  INTEGER,
    blocked_sessions  INTEGER,
    longest_block_s   REAL,
    open_tran         INTEGER,
    sql_memory_mb     REAL,
    databases_total   INTEGER,
    databases_online  INTEGER,
    PRIMARY KEY (server, collected_at)
);
CREATE TABLE IF NOT EXISTS database_snapshots (
    server        TEXT NOT NULL,
    collected_at  TEXT NOT NULL,
    database_name TEXT NOT NULL,
    state_desc    TEXT,
    is_system     INTEGER,
    data_mb       REAL,
    log_mb        REAL,
    log_used_pct  REAL,
    PRIMARY KEY (server, collected_at, database_name)
);
CREATE TABLE IF NOT EXISTS cpu_minutes (       -- SQL Server's own per-minute CPU history
    server        TEXT NOT NULL,
    event_time    TEXT NOT NULL,                -- start of the minute, this PC's local time
    sql_cpu_pct   REAL,
    other_cpu_pct REAL,
    PRIMARY KEY (server, event_time)
);
CREATE TABLE IF NOT EXISTS blocking_events (   -- sessions blocked >= BLOCKING_DETAIL_SECONDS
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    server          TEXT NOT NULL,
    blocked_since   TEXT NOT NULL,   -- when the wait began (this PC's local time)
    first_seen      TEXT NOT NULL,   -- first check that saw it over the threshold
    last_seen       TEXT NOT NULL,   -- last check that still saw it
    longest_wait_s  REAL,
    blocked_session INTEGER, blocked_login TEXT, blocked_host TEXT, blocked_program TEXT,
    database_name   TEXT, wait_type TEXT, wait_resource TEXT,
    blocked_sql     TEXT,            -- the statement that was waiting
    blocked_by      INTEGER,         -- the session it waited for directly
    head_blocker    INTEGER,         -- the session at the start of the chain
    head_login TEXT, head_host TEXT, head_program TEXT, head_status TEXT, head_open_tran INTEGER,
    head_sql        TEXT,            -- the head blocker's current / last statement
    chain           TEXT
);
CREATE INDEX IF NOT EXISTS ix_blocking_events         ON blocking_events (server, last_seen);
CREATE INDEX IF NOT EXISTS ix_server_snapshots_time   ON server_snapshots (collected_at);
CREATE INDEX IF NOT EXISTS ix_database_snapshots_time ON database_snapshots (collected_at);
CREATE INDEX IF NOT EXISTS ix_cpu_minutes_time        ON cpu_minutes (event_time);
"""


def default_history_path(settings: Settings) -> Path:
    """%USERPROFILE%\\CapstoneMonitor\\history.db unless HISTORY_STORE_PATH is set
    (next to the alert store, outside OneDrive and AppData - see alerts.default_store_path)."""
    if settings.history_store_path:
        return Path(settings.history_store_path)
    return Path.home() / "CapstoneMonitor" / "history.db"


def _iso(ts: datetime | pd.Timestamp) -> str:
    return pd.Timestamp(ts).to_pydatetime().replace(microsecond=0).isoformat(timespec="seconds")


def _py(value: Any) -> Any:
    """numpy / pandas scalars -> plain Python (sqlite3 cannot store numpy types); NaN -> NULL."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value.item() if hasattr(value, "item") else value


@dataclass
class Snapshot:
    """One collection of ONE server."""
    server: str                                   # friendly name
    collected_at: datetime
    connected: bool
    values: dict[str, Any] = field(default_factory=dict)            # SERVER_COLUMNS
    databases: pd.DataFrame = field(default_factory=pd.DataFrame)   # DATABASE_COLUMNS
    cpu: pd.DataFrame = field(default_factory=pd.DataFrame)         # CPU_COLUMNS (local time)
    blocking: list[dict] = field(default_factory=list)              # long-blocking events (BLOCKING_COLUMNS)
    error: str | None = None

    def summary_line(self) -> str:
        if not self.connected:
            return f"{self.collected_at:%H:%M} {self.server}: UNREACHABLE - {self.error}"
        v = self.values
        return (f"{self.collected_at:%H:%M} {self.server}: score {v.get('health_score')}, "
                f"{v.get('user_sessions')} sessions, {v.get('blocked_sessions')} blocked, "
                f"{v.get('databases_online')}/{v.get('databases_total')} databases online, "
                f"{len(self.cpu)} CPU minute(s)"
                + (f", {len(self.blocking)} long block(s) recorded" if self.blocking else ""))


@dataclass
class History:
    """Everything the history page shows for ONE server."""
    servers: pd.DataFrame        # collected_at, connected + SERVER_COLUMNS
    databases: pd.DataFrame      # collected_at + DATABASE_COLUMNS
    cpu: pd.DataFrame            # CPU_COLUMNS
    blocking: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=BLOCKING_COLUMNS))
    interval_seconds: int = 300
    first_snapshot: datetime | None = None
    last_snapshot: datetime | None = None
    sample: bool = False         # True = generated demo history

    @property
    def empty(self) -> bool:
        return self.servers.empty

    @property
    def span_days(self) -> float:
        if self.first_snapshot is None or self.last_snapshot is None:
            return 0.0
        return (self.last_snapshot - self.first_snapshot).total_seconds() / 86_400

    def since(self, start: datetime) -> "History":
        """The same history limited to snapshots from `start` onwards."""
        start = pd.Timestamp(start)
        return replace(self,
                       servers=self.servers[self.servers["collected_at"] >= start],
                       databases=self.databases[self.databases["collected_at"] >= start],
                       cpu=self.cpu[self.cpu["event_time"] >= start],
                       blocking=self.blocking[self.blocking["last_seen"] >= start]
                       if not self.blocking.empty else self.blocking)

    @classmethod
    def empty_history(cls, interval_seconds: int = 300) -> "History":
        return cls(servers=pd.DataFrame(columns=["collected_at", "connected", *SERVER_COLUMNS]),
                   databases=pd.DataFrame(columns=["collected_at", *DATABASE_COLUMNS]),
                   cpu=pd.DataFrame(columns=CPU_COLUMNS), interval_seconds=interval_seconds)


class HistoryStore:
    """history.db - written by the collector, read by the history page."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        # WAL: the page can read while the collector writes, without "database is locked".
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            with conn:              # commit on success, rollback on error
                yield conn
        finally:
            conn.close()

    def save(self, snap: Snapshot) -> None:
        ts = _iso(snap.collected_at)
        with self._conn() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO server_snapshots (server, collected_at, connected, "
                f"{', '.join(SERVER_COLUMNS)}) VALUES (?, ?, ?, {', '.join('?' * len(SERVER_COLUMNS))})",
                (snap.server, ts, int(snap.connected), *[_py(snap.values.get(c)) for c in SERVER_COLUMNS]))
            if not snap.databases.empty:
                conn.executemany(
                    "INSERT OR REPLACE INTO database_snapshots (server, collected_at, database_name, "
                    "state_desc, is_system, data_mb, log_mb, log_used_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(snap.server, ts, *[_py(r[c]) for c in DATABASE_COLUMNS])
                     for _, r in snap.databases.iterrows()])
            if not snap.cpu.empty:
                conn.executemany(       # IGNORE: the same minute arrives again in the next collection
                    "INSERT OR IGNORE INTO cpu_minutes (server, event_time, sql_cpu_pct, other_cpu_pct) "
                    "VALUES (?, ?, ?, ?)",
                    [(snap.server, _iso(r["event_time"]), _py(r["sql_cpu_pct"]), _py(r["other_cpu_pct"]))
                     for _, r in snap.cpu.iterrows()])
        if snap.blocking:
            self.record_blocking(snap.server, snap.blocking)

    def record_blocking(self, server: str, events: list[dict]) -> int:
        """Save long-blocking events. A wait that is already recorded (same blocked session,
        wait started within SAME_WAIT_SECONDS) is extended instead - last seen and the longest
        wait are updated, so one block is one row however many checks saw it.
        Returns the number of NEW events."""
        new = 0
        window = timedelta(seconds=SAME_WAIT_SECONDS)
        with self._conn() as conn:
            for e in events:
                since = pd.Timestamp(e["blocked_since"])
                existing = conn.execute(
                    "SELECT id FROM blocking_events WHERE server = ? AND blocked_session = ? "
                    "AND blocked_since BETWEEN ? AND ? ORDER BY blocked_since DESC",
                    (server, e["blocked_session"], _iso(since - window), _iso(since + window))).fetchone()
                if existing:
                    conn.execute(   # keep the FIRST statements captured - closest to the start of the block
                        "UPDATE blocking_events SET last_seen = ?, longest_wait_s = MAX(longest_wait_s, ?), "
                        "blocked_sql = COALESCE(blocked_sql, ?), head_sql = COALESCE(head_sql, ?) WHERE id = ?",
                        (_iso(e["last_seen"]), _py(e["longest_wait_s"]), e.get("blocked_sql"),
                         e.get("head_sql"), existing[0]))
                    continue
                values = [_iso(e[c]) if c in ("blocked_since", "first_seen", "last_seen") else _py(e.get(c))
                          for c in BLOCKING_COLUMNS]
                conn.execute(f"INSERT INTO blocking_events (server, {', '.join(BLOCKING_COLUMNS)}) "
                             f"VALUES (?, {', '.join('?' * len(BLOCKING_COLUMNS))})", (server, *values))
                new += 1
        return new

    def last_cpu_time(self, server: str) -> datetime | None:
        with self._conn() as conn:
            value = conn.execute("SELECT MAX(event_time) FROM cpu_minutes WHERE server = ?",
                                 (server,)).fetchone()[0]
        return datetime.fromisoformat(value) if value else None

    def purge(self, retention_days: int, now: datetime | None = None) -> int:
        """Delete everything older than the retention period; returns the rows deleted."""
        cutoff = _iso((now or datetime.now()) - timedelta(days=retention_days))
        with self._conn() as conn:
            return sum(conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)).rowcount
                       for table, col in (("server_snapshots", "collected_at"),
                                          ("database_snapshots", "collected_at"),
                                          ("cpu_minutes", "event_time"),
                                          ("blocking_events", "last_seen")))

    def load(self, server: str, since: datetime | None = None, interval_seconds: int = 300) -> History:
        """All stored history of one server (optionally only from `since`)."""
        start = _iso(since) if since else ""
        with self._conn() as conn:
            servers = pd.read_sql_query(
                "SELECT * FROM server_snapshots WHERE server = ? AND collected_at >= ? ORDER BY collected_at",
                conn, params=(server, start))
            dbs = pd.read_sql_query(
                "SELECT * FROM database_snapshots WHERE server = ? AND collected_at >= ? "
                "ORDER BY collected_at, database_name", conn, params=(server, start))
            cpu = pd.read_sql_query(
                "SELECT event_time, sql_cpu_pct, other_cpu_pct FROM cpu_minutes "
                "WHERE server = ? AND event_time >= ? ORDER BY event_time", conn, params=(server, start))
            blocking = pd.read_sql_query(
                f"SELECT {', '.join(BLOCKING_COLUMNS)} FROM blocking_events "
                "WHERE server = ? AND last_seen >= ? ORDER BY blocked_since", conn, params=(server, start))
            first, last = conn.execute(
                "SELECT MIN(collected_at), MAX(collected_at) FROM server_snapshots WHERE server = ?",
                (server,)).fetchone()
        servers["collected_at"] = pd.to_datetime(servers["collected_at"])
        servers["connected"] = servers["connected"].astype(bool)
        dbs["collected_at"] = pd.to_datetime(dbs["collected_at"])
        cpu["event_time"] = pd.to_datetime(cpu["event_time"])
        for col in ("blocked_since", "first_seen", "last_seen"):
            blocking[col] = pd.to_datetime(blocking[col])
        return History(servers=servers.drop(columns="server"), databases=dbs.drop(columns="server"),
                       cpu=cpu, blocking=blocking, interval_seconds=interval_seconds,
                       first_snapshot=datetime.fromisoformat(first) if first else None,
                       last_snapshot=datetime.fromisoformat(last) if last else None)


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
def candidates_frame(candidates: list[AlertCandidate]) -> pd.DataFrame:
    """Alert candidates in the shape health_score() expects (like the alert store's frame)."""
    return pd.DataFrame([{"severity": c.severity, "severity_rank": SEVERITY_ORDER[c.severity],
                          "name": c.name, "target": c.target} for c in candidates])


def server_clock_shift(local_now: datetime, server_now: Any) -> timedelta:
    """How far this PC's clock is ahead of SQL Server's (e.g. +5:30 when SQL Server runs
    on UTC and this PC on IST), rounded to 15 minutes - time zones are whole quarter
    hours, so the rounding removes collection jitter and every minute keeps one key."""
    if server_now is None or pd.isna(server_now):
        return timedelta(0)
    quarter = 15 * 60
    seconds = (pd.Timestamp(local_now) - pd.Timestamp(server_now)).total_seconds()
    return timedelta(seconds=round(seconds / quarter) * quarter)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def long_blocking_events(sessions: SessionsSnapshot, now: datetime, threshold_seconds: int) -> list[dict]:
    """One record per session blocked for at least `threshold_seconds`: the waiting statement,
    the session it waits for, and the HEAD blocker (who started the chain) with its status
    and current / last statement. Uses the blocking chains Module 3 already builds."""
    if not sessions.connected or sessions.chains.empty:
        return []
    by_id = sessions.sessions.drop_duplicates("session_id").set_index("session_id")
    events = []
    for c in sessions.chains.itertuples():
        wait = float(c.wait_seconds) if pd.notna(c.wait_seconds) else 0.0
        if wait < threshold_seconds:
            continue
        blocked = by_id.loc[c.session_id] if c.session_id in by_id.index else None
        head = by_id.loc[c.head_blocker] if c.head_blocker in by_id.index else None
        head_status, open_tran = None, None
        if head is not None:
            open_tran = int(head["open_transaction_count"] or 0)
            state = "running" if head.get("has_request") == 1 else (_text(head["session_status"]) or "unknown")
            head_status = state + (f" with {open_tran} open transaction(s)" if open_tran else "")
        events.append({
            "blocked_since": now - timedelta(seconds=wait), "first_seen": now, "last_seen": now,
            "longest_wait_s": wait, "blocked_session": int(c.session_id),
            "blocked_login": _text(c.login_name), "blocked_program": _text(c.program_name),
            "blocked_host": _text(blocked["host_name"]) if blocked is not None else None,
            "database_name": _text(c.database_name), "wait_type": _text(c.wait_type),
            "wait_resource": _text(c.wait_resource), "blocked_sql": _text(c.sql_text),
            "blocked_by": int(c.blocked_by), "head_blocker": int(c.head_blocker),
            "head_login": _text(head["login_name"]) if head is not None else None,
            "head_host": _text(head["host_name"]) if head is not None else None,
            "head_program": _text(head["program_name"]) if head is not None else None,
            "head_status": head_status, "head_open_tran": open_tran or None,
            "head_sql": _text(head["sql_text"]) if head is not None else None,
            "chain": _text(c.chain)})
    return events


def watch_blocking(settings: Settings, store: HistoryStore, now: datetime | None = None) -> list[str]:
    """The quick check between snapshots (every BLOCKING_CHECK_SECONDS): reads only the
    sessions DMVs and saves sessions blocked for BLOCKING_DETAIL_SECONDS or longer."""
    now = now or datetime.now().replace(microsecond=0)
    lines = []
    for t in server_targets(settings):
        sessions = collect_sessions(settings_for(settings, t.server))
        events = long_blocking_events(sessions, now, settings.blocking_detail_seconds)
        if events:
            new = store.record_blocking(t.name, events)
            heads = ", ".join(sorted({str(e["head_blocker"]) for e in events}))
            lines.append(f"{now:%H:%M:%S} {t.name}: {len(events)} session(s) blocked for "
                         f">= {settings.blocking_detail_seconds} s by head blocker {heads} ({new} new)")
    return lines


def collect_server(base: Settings, target: ServerTarget, now: datetime | None = None,
                   cpu_minutes: int = 15) -> tuple[Snapshot, list[AlertCandidate]]:
    """Read ONE server once (read-only) and return its snapshot plus its alert candidates."""
    s = settings_for(base, target.server)
    now = now or datetime.now().replace(microsecond=0)
    health = collect_health(s)
    if health.connected:
        sessions, backups = collect_sessions(s), collect_backups(s)
        memory, memory_error = collect_memory(s)
    else:                       # don't wait for three more connection time-outs
        sessions = SessionsSnapshot(connected=False, collected_at_local=now, error=health.error)
        backups = BackupSnapshot(connected=False, collected_at_local=now, error=health.error)
        memory, memory_error = None, None
    candidates = evaluate_rules(health, sessions, backups, memory, s, memory_error, server_name=target.name)
    score, _, _ = health_score(health.connected, candidates_frame(candidates))
    counts = {"health_score": score,
              "alerts_critical": sum(c.severity == "Critical" for c in candidates),
              "alerts_warning": sum(c.severity == "Warning" for c in candidates)}
    if not health.connected:
        return Snapshot(target.name, now, False, counts, error=health.error), candidates

    summary = summarize_sessions(sessions.sessions) if sessions.connected else {}
    dbs = health.databases
    values = {**counts,
              "user_sessions": summary.get("sessions"), "running_requests": summary.get("running"),
              "blocked_sessions": summary.get("blocked"), "longest_block_s": summary.get("longest_wait_s"),
              "open_tran": summary.get("open_tran"),
              "sql_memory_mb": health.server.get("sql_memory_in_use_mb"),
              "databases_total": len(dbs),
              "databases_online": int((dbs["state_desc"] == "ONLINE").sum()) if not dbs.empty else 0}
    databases = (dbs.rename(columns={"data_size_mb": "data_mb", "log_size_mb": "log_mb"})[DATABASE_COLUMNS]
                 if not dbs.empty else pd.DataFrame(columns=DATABASE_COLUMNS))

    cpu = collect_cpu_history(s, minutes=cpu_minutes)
    points = pd.DataFrame(columns=CPU_COLUMNS)
    if cpu.connected and not cpu.points.empty:
        shift = server_clock_shift(health.collected_at_local, health.server.get("server_time"))
        points = cpu.points[CPU_COLUMNS].copy()
        points["event_time"] = (pd.to_datetime(points["event_time"]) + shift).dt.floor("min")
        points = points.drop_duplicates("event_time")
    blocking = long_blocking_events(sessions, now, base.blocking_detail_seconds)
    return Snapshot(target.name, now, True, values, databases, points, blocking), candidates


def cpu_minutes_to_fetch(store: HistoryStore, server: str, now: datetime, interval_seconds: int) -> int:
    """Up to 4 hours on the first run or after a gap (the ring buffer keeps ~4 h), else just
    the minutes since the last saved one plus a small overlap."""
    last = store.last_cpu_time(server)
    if last is None:
        return 240
    minutes = int((now - last).total_seconds() // 60) + 5
    return max(interval_seconds // 60 + 5, min(240, minutes))


def run_once(settings: Settings, store: HistoryStore, alert_store: AlertStore | None = None,
             now: datetime | None = None) -> list[str]:
    """One collection cycle for EVERY monitored server. Returns log lines."""
    now = now or datetime.now().replace(microsecond=0)
    targets = server_targets(settings)
    lines: list[str] = []
    candidates: list[AlertCandidate] = []
    for t in targets:
        minutes = cpu_minutes_to_fetch(store, t.name, now, settings.history_interval_seconds)
        snap, server_candidates = collect_server(settings, t, now, cpu_minutes=minutes)
        store.save(snap)
        candidates += server_candidates
        lines.append(snap.summary_line())
    try:            # the same de-duplicating alert store the dashboard uses
        alert_store = alert_store or AlertStore(default_store_path(settings), legacy_server=targets[0].name)
        counts = alert_store.sync(candidates, now=now, history_days=settings.alert_history_days)
        lines.append(f"{now:%H:%M} alerts: {len(candidates)} active "
                     f"({counts['opened'] + counts['reopened']} new, {counts['resolved']} resolved)")
    except Exception as exc:              # a broken alert store must not stop the history
        lines.append(f"{now:%H:%M} alert store unavailable: {exc}")
    purged = store.purge(settings.history_retention_days, now)
    if purged:
        lines.append(f"{now:%H:%M} deleted {purged} row(s) older than {settings.history_retention_days} days")
    return lines


# ---------------------------------------------------------------------------
# Analysis (pure functions - used by the page, easy to unit-test)
# ---------------------------------------------------------------------------
def resample(df: pd.DataFrame, time_col: str, columns: list[str], freq: str | None,
             how: str = "mean") -> pd.DataFrame:
    """Average (or max) per time bucket, e.g. hourly for a 7-day chart. freq None = unchanged."""
    if df.empty or freq is None:
        return df
    out = df.set_index(time_col)[columns].apply(pd.to_numeric, errors="coerce").resample(freq).agg(how)
    return out.dropna(how="all").reset_index()


def blocking_incidents(servers: pd.DataFrame, interval_seconds: int) -> pd.DataFrame:
    """Group consecutive snapshots that saw blocked sessions into incidents."""
    columns = ["start", "end", "minutes", "max_blocked", "longest_wait_s"]
    if servers.empty:
        return pd.DataFrame(columns=columns)
    gap = pd.Timedelta(seconds=interval_seconds * 2.5)        # one missed snapshot is tolerated
    step = pd.Timedelta(seconds=interval_seconds)
    rows: list[dict] = []
    current: dict | None = None
    for r in servers.sort_values("collected_at").itertuples():
        blocked = r.blocked_sessions if pd.notna(r.blocked_sessions) else 0
        if blocked > 0:
            wait = float(r.longest_block_s) if pd.notna(r.longest_block_s) else 0.0
            if current is not None and r.collected_at - current["end"] <= gap:
                current["end"] = r.collected_at
                current["max_blocked"] = max(current["max_blocked"], int(blocked))
                current["longest_wait_s"] = max(current["longest_wait_s"], wait)
            else:
                if current is not None:
                    rows.append(current)
                current = {"start": r.collected_at, "end": r.collected_at,
                           "max_blocked": int(blocked), "longest_wait_s": wait}
        elif current is not None:
            rows.append(current)
            current = None
    if current is not None:
        rows.append(current)
    if not rows:
        return pd.DataFrame(columns=columns)
    out = pd.DataFrame(rows, columns=["start", "end", "max_blocked", "longest_wait_s"])
    # Each snapshot stands for one interval, so a single sighting counts as one interval.
    out["minutes"] = ((out["end"] - out["start"] + step).dt.total_seconds() / 60).round()
    return out[columns]


def unreachable_minutes(servers: pd.DataFrame, interval_seconds: int) -> float:
    if servers.empty:
        return 0.0
    return float((~servers["connected"].astype(bool)).sum() * interval_seconds / 60)


def growth_table(databases: pd.DataFrame, project_days: int = 30) -> pd.DataFrame:
    """Per database: total size (data + log) at the start and end of the range, the growth
    rate per day (least-squares line through every snapshot, so one odd reading does not
    distort it) and the size projected `project_days` ahead."""
    columns = ["database_name", "is_system", "start_mb", "end_mb", "growth_mb", "mb_per_day",
               "projected_mb"]
    rows = []
    for name, g in databases.dropna(subset=["data_mb"]).groupby("database_name"):
        g = g.sort_values("collected_at")
        total = g["data_mb"].astype(float) + g["log_mb"].fillna(0).astype(float)
        days = (g["collected_at"] - g["collected_at"].iloc[0]).dt.total_seconds() / 86_400
        slope = (float(np.polyfit(days, total, 1)[0])
                 if len(g) >= 3 and days.iloc[-1] >= 1 / 24 else float("nan"))
        rows.append({"database_name": name, "is_system": int(g["is_system"].iloc[-1] or 0),
                     "start_mb": total.iloc[0], "end_mb": total.iloc[-1],
                     "growth_mb": total.iloc[-1] - total.iloc[0], "mb_per_day": slope,
                     "projected_mb": total.iloc[-1] + slope * project_days if not np.isnan(slope) else np.nan})
    out = pd.DataFrame(rows, columns=columns)
    return out.sort_values("mb_per_day", ascending=False, na_position="last").reset_index(drop=True)


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def busiest_hours(cpu: pd.DataFrame) -> pd.DataFrame:
    """Average SQL Server CPU % by weekday (rows Mon-Sun) and hour of day (columns 0-23)."""
    if cpu.empty:
        return pd.DataFrame(index=range(7), columns=range(24), dtype=float)
    df = cpu.assign(weekday=cpu["event_time"].dt.day_of_week, hour=cpu["event_time"].dt.hour,
                    sql=pd.to_numeric(cpu["sql_cpu_pct"], errors="coerce"))
    return (df.pivot_table(index="weekday", columns="hour", values="sql", aggfunc="mean")
            .reindex(index=range(7), columns=range(24)))


def quietest_hour(pivot: pd.DataFrame) -> tuple[str, int, float] | None:
    """(weekday, hour, average CPU) of the quietest hour - a candidate maintenance window."""
    stacked = pivot.stack().dropna()
    if stacked.empty:
        return None
    weekday, hour = stacked.idxmin()
    return WEEKDAYS[int(weekday)], int(hour), float(stacked.min())
