"""
Application settings.

DBA analogy: this is the "Connect to Server" dialog of SSMS, but stored in a
file (.env) instead of typed in every time. Values are read once from the
.env file in the project folder; anything missing falls back to a safe default.

Only Windows Authentication is supported on purpose - no passwords are ever
stored in files or source code.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env from the project folder (does nothing if the file does not exist).
load_dotenv(PROJECT_ROOT / ".env")


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    # --- Connection (Windows Authentication only) ---------------------------
    server: str = "localhost"
    database: str = "master"
    driver: str = "ODBC Driver 17 for SQL Server"
    encrypt: bool = False
    trust_server_certificate: bool = True
    login_timeout_seconds: int = 5
    query_timeout_seconds: int = 30
    app_name: str = "DBMonitor-Capstone"

    # --- Dashboard ------------------------------------------------------------
    cache_seconds: int = 30          # how long a collected snapshot is reused

    # --- Health thresholds (Module 1) -----------------------------------------
    log_used_warning_pct: int = 75   # transaction log "Percent Log Used"

    # --- Session thresholds (Module 3) ----------------------------------------
    blocking_warning_seconds: int = 30   # a session blocked longer than this = prolonged blocking
    long_running_seconds: int = 60       # a request running longer than this = long-running

    # --- Backup policy (Module 4) -----------------------------------------------
    full_backup_max_hours: int = 168     # a FULL backup at least every 7 days
    data_backup_max_hours: int = 24      # a FULL or DIFFERENTIAL at least every 24 h
    log_backup_max_minutes: int = 60     # a LOG backup at least every 60 min (FULL / BULK_LOGGED)

    # --- Alerts (Module 5) -------------------------------------------------------
    log_used_critical_pct: int = 90      # transaction log nearly full -> Critical
    memory_window_minutes: int = 15      # look-back for low-memory notifications
    alert_history_days: int = 7          # resolved alerts are kept this long
    alert_store_path: str = ""           # SQLite file; empty = %USERPROFILE%\CapstoneMonitor\alerts.db

    # --- Monitored servers (multi-instance) --------------------------------------
    servers: tuple["ServerTarget", ...] = ()   # from SQL_SERVERS; empty = just SQL_SERVER

    # --- Public demo ----------------------------------------------------------------
    # DEMO_MODE=true: fictional servers with clearly labelled SAMPLE data; the app
    # never connects to SQL Server (used for the public Streamlit Community Cloud link).
    demo_mode: bool = False


@dataclass(frozen=True)
class ServerTarget:
    """One monitored SQL Server instance: a friendly name and its connection name."""
    name: str          # e.g. "Primary"
    server: str        # e.g. "localhost" or "localhost\\SQLDEV2"

    @property
    def label(self) -> str:
        return self.name if self.name == self.server else f"{self.name} · {self.server}"


def parse_servers(value: str) -> tuple[ServerTarget, ...]:
    """
    Parse SQL_SERVERS, e.g.  "Primary=localhost; Secondary=localhost\\SQLDEV2".
    An entry without "=" uses the server name as its friendly name.
    Blank entries and duplicate names are ignored (first one wins).
    """
    targets: list[ServerTarget] = []
    seen: set[str] = set()
    for part in (value or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, server = part.partition("=")
        name, server = (name.strip(), server.strip()) if sep else (part, part)
        if not server or not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        targets.append(ServerTarget(name, server))
    return tuple(targets)


# Fictional servers used only in DEMO_MODE (example.local is a reserved, non-real domain).
DEMO_TARGETS: tuple[ServerTarget, ...] = (
    ServerTarget("DEMO-PROD-01", "demo-prod-01.example.local"),
    ServerTarget("DEMO-PROD-02", "demo-prod-02.example.local\\FINANCE"),
    ServerTarget("DEMO-UAT-01", "demo-uat-01.example.local"),
    ServerTarget("DEMO-REPORT-01", "demo-report-01.example.local"),
    ServerTarget("DEMO-DEV-01", "demo-dev-01.example.local"),
)


def server_targets(settings: Settings) -> tuple[ServerTarget, ...]:
    """All monitored servers; falls back to the single SQL_SERVER setting.
    In DEMO_MODE the fictional demo servers are used instead."""
    if settings.demo_mode:
        return DEMO_TARGETS
    return settings.servers or (ServerTarget(settings.server, settings.server),)


LOCAL_HOST_NAMES = {"localhost", ".", "(local)", "127.0.0.1", "::1"}


def local_computer_name() -> str:
    return os.getenv("COMPUTERNAME") or socket.gethostname()


def server_aliases(t: ServerTarget) -> list[str]:
    """
    Every name a user might type for this server (lower case):
    friendly name, connection name, instance name, and - for a local server -
    the real SQL Server name (computer name + instance), e.g.
    'localhost'          -> 'sqlhost-01'
    'localhost\\SQLDEV2' -> 'sqlhost-01\\sqldev2'
    Derived from the list only - no server is contacted.
    """
    host, sep, instance = t.server.partition("\\")
    names = [t.name, t.server]
    if instance:
        names.append(instance)
    if host.strip().lower() in LOCAL_HOST_NAMES:
        names.append(local_computer_name() + (sep + instance if sep else ""))
    return list(dict.fromkeys(n.strip().lower() for n in names if n.strip()))


def search_servers(targets: tuple[ServerTarget, ...], query: str) -> list[ServerTarget]:
    """
    Find servers by friendly name, connection name, instance name or real
    server (computer) name - case-insensitive.
    Best matches first: exact -> starts with -> contains.
    Only the server LIST is searched - no server is contacted - so this stays
    instant even with thousands of servers.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    ranked = []
    for position, t in enumerate(targets):
        aliases = server_aliases(t)
        if q in aliases:
            score = 0
        elif any(a.startswith(q) for a in aliases):
            score = 1
        elif any(q in a for a in aliases):
            score = 2
        else:
            continue
        ranked.append((score, position, t))
    return [t for _, _, t in sorted(ranked, key=lambda r: (r[0], r[1]))]


def exact_server_match(targets: tuple[ServerTarget, ...], query: str) -> ServerTarget | None:
    """The one server whose name/alias is exactly the query (None if zero or several)."""
    q = (query or "").strip().lower()
    hits = [t for t in targets if q and q in server_aliases(t)]
    return hits[0] if len(hits) == 1 else None


def settings_for(settings: Settings, server: str) -> Settings:
    """The same settings, pointed at another instance (used by every collector)."""
    return replace(settings, server=server)


@lru_cache(maxsize=8)
def odbc_driver_available(driver: str) -> bool:
    """Is this ODBC driver installed here? (False on hosts such as Streamlit
    Community Cloud, where real SQL Server monitoring is impossible.)"""
    try:
        import pyodbc
        return driver in pyodbc.drivers()
    except Exception:
        return False


def _demo_mode(driver: str) -> bool:
    """DEMO_MODE=true/false wins; if it is not set, demo mode switches on
    automatically when the SQL Server ODBC driver is missing (e.g. a cloud host),
    so a public deployment always shows the labelled sample data."""
    explicit = os.getenv("DEMO_MODE", "").strip()
    if explicit:
        return explicit.lower() in {"1", "true", "yes", "y", "on"}
    return not odbc_driver_available(driver)


def get_settings() -> Settings:
    """Build Settings from environment variables / .env (see .env.example)."""
    defaults = Settings()
    return Settings(
        server=_env_str("SQL_SERVER", defaults.server),
        database=_env_str("SQL_DATABASE", defaults.database),
        driver=_env_str("SQL_ODBC_DRIVER", defaults.driver),
        encrypt=_env_bool("SQL_ENCRYPT", defaults.encrypt),
        trust_server_certificate=_env_bool(
            "SQL_TRUST_SERVER_CERTIFICATE", defaults.trust_server_certificate
        ),
        login_timeout_seconds=_env_int("SQL_LOGIN_TIMEOUT", defaults.login_timeout_seconds),
        query_timeout_seconds=_env_int("SQL_QUERY_TIMEOUT", defaults.query_timeout_seconds),
        app_name=_env_str("SQL_APP_NAME", defaults.app_name),
        cache_seconds=_env_int("DASHBOARD_CACHE_SECONDS", defaults.cache_seconds),
        log_used_warning_pct=_env_int("ALERT_LOG_USED_PCT", defaults.log_used_warning_pct),
        blocking_warning_seconds=_env_int("ALERT_BLOCKING_SECONDS",
                                          defaults.blocking_warning_seconds),
        long_running_seconds=_env_int("ALERT_LONG_RUNNING_SECONDS", defaults.long_running_seconds),
        full_backup_max_hours=_env_int("ALERT_FULL_BACKUP_HOURS", defaults.full_backup_max_hours),
        data_backup_max_hours=_env_int("ALERT_DATA_BACKUP_HOURS", defaults.data_backup_max_hours),
        log_backup_max_minutes=_env_int("ALERT_LOG_BACKUP_MINUTES", defaults.log_backup_max_minutes),
        log_used_critical_pct=_env_int("ALERT_LOG_USED_CRITICAL_PCT", defaults.log_used_critical_pct),
        memory_window_minutes=_env_int("ALERT_MEMORY_WINDOW_MINUTES", defaults.memory_window_minutes),
        alert_history_days=_env_int("ALERT_HISTORY_DAYS", defaults.alert_history_days),
        alert_store_path=_env_str("ALERT_STORE_PATH", defaults.alert_store_path),
        servers=parse_servers(os.getenv("SQL_SERVERS", "")),
        demo_mode=_demo_mode(_env_str("SQL_ODBC_DRIVER", defaults.driver)),
    )
