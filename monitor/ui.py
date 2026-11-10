"""
Shared dashboard pieces used by every page: colours, chart styling, the
sidebar (server picker, connection indicator, alert count) and the cached
data loaders.

Multi-server: every loader takes the connection name of the server it reads,
so each instance has its own cache entry. The server shown on the pages is
chosen in the sidebar (searchable) or with ?server=<name> in the address bar.

Caching: each loader keeps its result for DASHBOARD_CACHE_SECONDS so clicking
around the page does not re-query SQL Server every time. "Refresh now" clears it.
"""

from __future__ import annotations

import html

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from monitor.collectors import (BackupSnapshot, HealthSnapshot, QueryPerfSnapshot,
                                SessionsSnapshot, collect_backups, collect_health,
                                collect_query_stats, collect_sessions)
from monitor.config import ServerTarget, get_settings, server_targets, settings_for

# Read at import only for the cache lifetime; everything else calls get_settings()
# at run time so a changed environment is always honoured.
settings = get_settings()

# ---------------------------------------------------------------------------
# Colour roles (validated reference palette, light mode)
# ---------------------------------------------------------------------------
SERIES_1 = "#2a78d6"      # blue
SERIES_2 = "#eb6834"      # orange
STATUS_GOOD, STATUS_WARNING, STATUS_CRITICAL = "#0ca30c", "#fab219", "#d03b3b"
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'
# Categorical slots in FIXED order (validated for colour-blind separation of neighbours);
# a series keeps its colour when others are filtered out.
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
# Sequential single-hue ramp (light = low, dark = high) for heatmaps.
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

STATUS_LABEL = {"Healthy": "✅ Healthy", "Warning": "⚠️ Warning", "Critical": "⛔ Critical"}
CARD_WIDTH = 160          # fixed-width metric cards wrap on narrow screens

CSS = """
<style>
  .block-container {padding-top: 2rem;}
  .app-subtitle {color: #52514e; margin-top: -0.6rem; margin-bottom: 1rem;}
  .conn-pill {display:inline-flex; align-items:center; gap:.45rem; padding:.3rem .7rem;
              border-radius:999px; font-weight:600; font-size:.9rem;
              border:1px solid rgba(11,11,11,.10); background:#fcfcfb; color:#0b0b0b;}
  .conn-dot {width:.65rem; height:.65rem; border-radius:50%;}
  .footnote {color:#898781; font-size:.85rem;}
  [data-testid="stMetricValue"] {font-size: 1.6rem; white-space: nowrap;}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Server selection - search first, then work on ONE server
# ---------------------------------------------------------------------------
SELECTED_KEY = "selected_server"
SEARCH_PAGE = "views/server_search.py"


def targets() -> tuple[ServerTarget, ...]:
    return server_targets(get_settings())


def current_target() -> ServerTarget | None:
    """The server chosen on the Find-a-server page (or ?server=<name> in the URL).
    None until a server has been chosen - pages then ask the user to search."""
    all_targets = targets()
    if len(all_targets) == 1:                      # nothing to choose from
        return all_targets[0]
    by_name = {t.name.lower(): t for t in all_targets}
    chosen = st.session_state.get(SELECTED_KEY) or st.query_params.get("server") or ""
    target = by_name.get(str(chosen).lower())
    if target is None and get_settings().demo_mode:
        target = all_targets[0]            # demo: open with a sample server, every page has data
    if target is not None and st.session_state.get(SELECTED_KEY) != target.name:
        st.session_state[SELECTED_KEY] = target.name   # remember a ?server= link
    return target


def current_server() -> str | None:
    """Connection name of the chosen server, e.g. 'localhost\\SQLDEV2' (None if none chosen)."""
    t = current_target()
    return t.server if t else None


def select_server(name: str) -> None:
    """Make a server the chosen one (search results, links)."""
    st.session_state[SELECTED_KEY] = name
    st.query_params["server"] = name


def clear_server() -> None:
    st.session_state.pop(SELECTED_KEY, None)
    if "server" in st.query_params:
        del st.query_params["server"]


def require_server() -> ServerTarget:
    """Stop the page with a 'search first' hint when no server is chosen."""
    t = current_target()
    if t is None:
        st.info("No server selected yet. Search for the server you want to look at.", icon="🔎")
        st.page_link(SEARCH_PAGE, label="Find a server", icon="🔎")
        st.stop()
    return t


def page_header(title: str, show_server: bool = True) -> None:
    st.title(title)
    server_part = ""
    t = current_target() if show_server else None
    if t is not None:
        server_part = (f" · showing <b>{html.escape(t.name)}</b>"
                       + (f" ({html.escape(t.server)})" if t.server != t.name else ""))
    st.markdown("<div class='app-subtitle'>Database Server Monitoring and Performance "
                f"Analysis System{server_part}</div>", unsafe_allow_html=True)


def footnote(text: str) -> None:
    st.markdown(f"<div class='footnote'>{text}</div>", unsafe_allow_html=True)


def connection_pill(connected: bool, server: str) -> str:
    colour, text = (STATUS_GOOD, "Connected") if connected else (STATUS_CRITICAL, "Disconnected")
    return (f'<span class="conn-pill"><span class="conn-dot" style="background:{colour}"></span>'
            f'{text} · {html.escape(server)}</span>')


def style_figure(fig: go.Figure, height: int, left_margin: int = 90) -> go.Figure:
    """Recessive axes and grid, system font, transparent background."""
    fig.update_layout(
        height=height, margin=dict(l=left_margin, r=24, t=36, b=28),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, color=INK_2, size=13),
        hoverlabel=dict(bgcolor=SURFACE, bordercolor=GRID, font=dict(color=INK, family=FONT)),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title_text="",
                    traceorder="normal"),
        bargap=0.35,
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False, linecolor=AXIS, tickfont=dict(color=MUTED))
    fig.update_yaxes(showgrid=False, linecolor=AXIS, tickfont=dict(color=INK_2),
                     automargin=True, ticksuffix="  ")
    return fig


def style_altair(chart, height: int):
    """Same recessive look as the Plotly charts, for clickable Altair charts."""
    return (chart.properties(height=height, background="transparent")
            .configure(font=FONT)
            .configure_axis(labelColor=INK_2, titleColor=MUTED, gridColor=GRID, domainColor=AXIS,
                            tickColor=AXIS, labelFontSize=12, titleFontSize=12, labelLimit=220)
            .configure_axisY(grid=False)
            .configure_legend(labelColor=INK_2, orient="top", title=None)
            .configure_view(strokeWidth=0))


def altair_pick(event, field: str, param: str = "pick"):
    """Value of `field` for the clicked item of a selection_point named `param`."""
    if not event:
        return None
    selection = getattr(event, "selection", None)
    if selection is None and isinstance(event, dict):
        selection = event.get("selection", {})
    values = (selection or {}).get(param) or []
    if isinstance(values, dict):
        values = [values]
    return values[0].get(field) if values else None


def show_unavailable(error: str | None, detail: str | None, attempted_at,
                     target: ServerTarget | None = None) -> None:
    """Standard 'SQL Server is unavailable' state used by every page."""
    t = target or current_target()
    st.error(f"**SQL Server {html.escape(t.label)} is unavailable.** {error}", icon="⛔")
    with st.expander("Troubleshooting steps"):
        service = ("SQL Server (MSSQLSERVER)" if "\\" not in t.server
                   else f"SQL Server ({t.server.split(chr(92), 1)[1]})")
        st.markdown(
            f"1. Open **Services** (`services.msc`) and check **{service}** is *Running* "
            "(after a PC restart it can take a few minutes - it is a delayed-start service).\n"
            f"2. In SSMS, connect to **{html.escape(t.server)}** with Windows Authentication.\n"
            "3. Check `SQL_SERVERS` / `SQL_ODBC_DRIVER` in the `.env` file.\n"
            "4. Press **Refresh now** in the sidebar."
        )
        if detail:
            st.code(detail, language=None)
    st.caption(f"Last attempt: {attempted_at:%Y-%m-%d %H:%M:%S} (this PC's clock)")


# ---------------------------------------------------------------------------
# Cached loaders - one cache entry per server
# ---------------------------------------------------------------------------
def demo_mode() -> bool:
    """True when DEMO_MODE=true: sample data only, SQL Server is never contacted."""
    return get_settings().demo_mode


@st.cache_data(ttl=settings.cache_seconds, show_spinner="Collecting data from SQL Server…")
def load_health(server: str, demo: bool = False) -> HealthSnapshot:
    if demo:
        from monitor import demo_data
        return demo_data.health(server)
    return collect_health(settings_for(get_settings(), server))


@st.cache_data(ttl=settings.cache_seconds, show_spinner="Reading the plan cache…")
def load_query_stats(server: str, metric: str, demo: bool = False) -> QueryPerfSnapshot:
    if demo:
        from monitor import demo_data
        return demo_data.query_stats(server, metric)
    return collect_query_stats(metric, settings=settings_for(get_settings(), server))


# Sessions change second by second, so they are cached for 5 s only.
@st.cache_data(ttl=5, show_spinner=False)
def load_sessions(server: str, demo: bool = False) -> SessionsSnapshot:
    if demo:
        from monitor import demo_data
        return demo_data.sessions(server)
    return collect_sessions(settings_for(get_settings(), server))


@st.cache_data(ttl=settings.cache_seconds, show_spinner="Reading msdb backup history…")
def load_backups(server: str, demo: bool = False) -> BackupSnapshot:
    if demo:
        from monitor import demo_data
        return demo_data.backups(server)
    return collect_backups(settings_for(get_settings(), server))


@st.cache_data(ttl=60, show_spinner=False)
def load_cpu(server: str, demo: bool = False):
    if demo:
        from monitor import demo_data
        return demo_data.cpu_history(server)
    from monitor.collectors import collect_cpu_history
    return collect_cpu_history(settings_for(get_settings(), server))


def cpu_of(server: str):
    return load_cpu(server, demo_mode())


@st.cache_data(ttl=60, show_spinner="Reading performance history…")
def load_history(name: str, server: str, demo: bool = False):
    """Module 6: the stored snapshots of one server (generated sample history in demo mode).
    Reads history.db only - SQL Server is not contacted."""
    from monitor.history import History, HistoryStore, default_history_path
    if demo:
        from monitor import demo_data
        return demo_data.history(server, name)
    s = get_settings()
    path = default_history_path(s)
    if not path.exists():
        return History.empty_history(s.history_interval_seconds)
    return HistoryStore(path).load(name, interval_seconds=s.history_interval_seconds)


def history_of(t: ServerTarget):
    return load_history(t.name, t.server, demo_mode())


# The pages call these; the demo flag is part of the cache key, so real and
# sample data can never be mixed up.
def health_of(server: str) -> HealthSnapshot:
    return load_health(server, demo_mode())


def query_stats_of(server: str, metric: str) -> QueryPerfSnapshot:
    return load_query_stats(server, metric, demo_mode())


def sessions_of(server: str) -> SessionsSnapshot:
    return load_sessions(server, demo_mode())


def backups_of(server: str) -> BackupSnapshot:
    return load_backups(server, demo_mode())


@st.cache_data(ttl=settings.cache_seconds, show_spinner=False)
def evaluate_alerts() -> dict:
    """Run every alert rule for EVERY monitored server, once per cache period,
    and sync the alert store.

    Cached on purpose: however often pages are refreshed, the rules run at most
    once per DASHBOARD_CACHE_SECONDS, so alert counters stay meaningful.
    """
    from datetime import datetime

    from monitor.alerts import AlertStore, collect_memory, default_store_path, evaluate_rules

    base = get_settings()
    all_targets = server_targets(base)
    candidates = []
    for t in all_targets:
        s = settings_for(base, t.server)
        health = health_of(t.server)
        if base.demo_mode:
            from monitor import demo_data
            memory, memory_error = (demo_data.memory(t.server) if health.connected else None), None
        else:
            memory, memory_error = collect_memory(s) if health.connected else (None, None)
        candidates += evaluate_rules(health, sessions_of(t.server), backups_of(t.server),
                                     memory, s, memory_error, server_name=t.name)
    try:
        store = AlertStore(default_store_path(base), legacy_server=all_targets[0].name)
        counts = store.sync(candidates, history_days=base.alert_history_days)
        store_error = None
    except Exception as exc:          # a broken store must never break the dashboard
        counts, store_error = {}, f"Alert store unavailable: {exc}"
    return {"evaluated_at": datetime.now(), "current": len(candidates),
            "store_error": store_error, **counts}


def alert_store():
    from monitor.alerts import AlertStore, default_store_path
    return AlertStore(default_store_path(get_settings()), legacy_server=targets()[0].name)


def active_alerts(server_name: str | None = None) -> pd.DataFrame:
    """Active alerts - of one server if a name is given (empty if the store is unavailable)."""
    try:
        df = alert_store().frame("ACTIVE")
    except Exception:
        return pd.DataFrame()
    if server_name is not None and not df.empty:
        df = df[df["server"] == server_name]
    return df


# ---------------------------------------------------------------------------
# Sidebar - shows ONLY the chosen server (no list of all servers)
# ---------------------------------------------------------------------------
def render_sidebar_status() -> None:
    """Chosen server, connection indicator, its alert count, change-server link, refresh."""
    current = get_settings()
    selected = current_target()
    evaluate_alerts()
    with st.sidebar:
        if selected is None:
            st.markdown("**No server selected**")
            st.page_link(SEARCH_PAGE, label="Find a server", icon="🔎")
        else:
            snap = health_of(selected.server)
            st.markdown(f"Server: **{html.escape(selected.name)}**", unsafe_allow_html=True)
            st.markdown(connection_pill(snap.connected, selected.server), unsafe_allow_html=True)
            active = active_alerts(selected.name)
            if not active.empty and "is_snoozed" in active:
                active = active[~active["is_snoozed"]]          # snoozed alerts don't nag
            critical = int((active["severity"] == "Critical").sum()) if not active.empty else 0
            label = (f"{len(active)} active alert{'s' if len(active) != 1 else ''}"
                     + (f" · {critical} critical" if critical else ""))
            st.page_link("views/alerts.py", label=label, icon="🔔" if len(active) else "✅")
            if len(targets()) > 1:
                st.page_link(SEARCH_PAGE, label="Change server", icon="🔎")
        st.write("")
        if st.button("Refresh now", icon="🔄", type="primary", width="stretch"):
            st.cache_data.clear()
            st.rerun()
        st.caption(f"Data is reused for {current.cache_seconds} s between refreshes.")
