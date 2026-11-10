"""
Find a server - the home page.

Search-first, like a production monitoring tool with thousands of servers:
nothing is listed until you search. Typing searches only the server LIST
(SQL_SERVERS) - no server is contacted - so it stays instant at any scale.
  * exactly one match  -> that server is selected and shown
  * several matches    -> names only (no data); pick one
  * a chosen server    -> ONLY that server's summary, plus links to its pages
"""

import html

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from monitor import ui
from monitor.alerts import health_score
from monitor.collectors import (BackupPolicy, evaluate_backups, format_size_mb, format_uptime,
                                summarize_backups, summarize_databases, summarize_sessions,
                                version_label)
from monitor.config import ServerTarget, exact_server_match, get_settings, search_servers

settings = get_settings()
MAX_RESULTS = 10

ui.page_header("Find a Server", show_server=False)

# Streamlit applies a text box when Enter is pressed or the box loses focus.
# The Search button does the latter, so either way works for the user.
examples = ", ".join(t.name for t in ui.targets()[:2])
with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
    query = st.text_input("Search server", key="srv_search",
                          placeholder=f"Server name, connection or computer name - e.g. {examples}",
                          label_visibility="collapsed")
    st.button("Search", icon="🔎", type="primary", key="srv_search_go")
st.caption("Type a name, then press **Enter** or click **Search**.")

if ui.demo_mode():
    # Demo only: quick-pick buttons so visitors can explore every sample server.
    # (The real dashboard never lists servers - it is search-first.)
    st.markdown("**Demo servers to try** - each one shows a different situation:")
    hints = {"DEMO-PROD-01": "blocking chain", "DEMO-PROD-02": "log almost full",
             "DEMO-UAT-01": "never backed up", "DEMO-REPORT-01": "offline DB · long query",
             "DEMO-DEV-01": "unreachable"}
    with st.container(horizontal=True, wrap=True, gap="small"):
        for t in ui.targets():
            st.button(f"{t.name} · {hints.get(t.name, '')}", key=f"demo_{t.name}",
                      on_click=ui.select_server, args=(t.name,))


RATING_COLOUR = {"Good": ui.STATUS_GOOD, "Fair": ui.STATUS_WARNING, "Poor": ui.STATUS_CRITICAL}
RATING_ICON = {"Good": "✅", "Fair": "⚠️", "Poor": "⛔"}


def score_gauge(score: int, rating: str, reasons: list[str]) -> None:
    """0-100 health score as a gauge, with the reasons that lowered it."""
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=score, number={"font": {"size": 34, "color": ui.INK}},
        gauge={"axis": {"range": [0, 100], "tickwidth": 0, "tickcolor": ui.AXIS,
                        "tickfont": {"color": ui.MUTED, "size": 10}},
               "bar": {"color": RATING_COLOUR[rating], "thickness": 0.8},
               "bgcolor": ui.GRID, "borderwidth": 0}))
    fig.update_layout(height=150, margin=dict(l=30, r=34, t=14, b=0),
                      paper_bgcolor="rgba(0,0,0,0)", font=dict(family=ui.FONT))
    st.plotly_chart(fig, theme=None, config={"displayModeBar": False}, key="score_gauge")
    st.markdown(f"<div style='text-align:center;margin-top:-0.6rem'><b>{RATING_ICON[rating]} Health "
                f"score: {rating}</b></div>", unsafe_allow_html=True)
    with st.expander("Why this score?"):
        if reasons:
            st.markdown("Start at 100, minus each active alert:\n" + "\n".join(f"- {r}" for r in reasons))
        else:
            st.markdown("No active alerts - nothing lowers the score.")


def card(label, value, page: str, link_label: str, help: str | None = None, chart=None) -> None:
    """A clickable summary card: the number, an optional 60-minute line, and a link."""
    with st.container(border=True, width=205):
        st.metric(label, value, help=help, chart_data=chart, chart_type="line")
        st.page_link(page, label=link_label, icon=":material/arrow_forward:")


def cpu_trend(points: pd.DataFrame) -> None:
    """Stacked CPU % for the last 60 minutes: SQL Server vs other processes."""
    fig = go.Figure()
    for col, name, colour in (("sql_cpu_pct", "SQL Server", ui.SERIES_1),
                              ("other_cpu_pct", "Other processes", ui.MUTED)):
        fig.add_scatter(x=points["event_time"], y=points[col], name=name, mode="lines",
                        stackgroup="cpu", line=dict(width=2, color=colour),
                        hovertemplate="%{x|%H:%M}  " + name + ": %{y:.0f} %<extra></extra>")
    fig.update_yaxes(range=[0, 100], ticksuffix=" %")
    with st.container(border=True):
        st.markdown("**CPU - last 60 minutes** (SQL Server clock) · hover for exact values")
        st.plotly_chart(ui.style_figure(fig, 220, left_margin=50), theme=None,
                        config={"displayModeBar": False}, key="cpu_trend")


def show_server_summary(t: ServerTarget) -> None:
    """Everything about ONE server - and nothing about any other."""
    health = ui.health_of(t.server)
    with st.container(border=True):
        colour, state = ((ui.STATUS_GOOD, "Online") if health.connected
                         else (ui.STATUS_CRITICAL, "Unreachable"))
        st.markdown(
            f"<div style='display:flex;justify-content:space-between;align-items:center'>"
            f"<b style='font-size:1.4rem'>Showing: {html.escape(t.name)}</b>"
            f"<span class='conn-pill'><span class='conn-dot' style='background:{colour}'></span>"
            f"{state}</span></div><div class='footnote'>{html.escape(t.server)}</div>",
            unsafe_allow_html=True)

        ui.evaluate_alerts()
        alerts = ui.active_alerts(t.name)
        crit = int((alerts["severity"] == "Critical").sum()) if not alerts.empty else 0
        warn = int((alerts["severity"] == "Warning").sum()) if not alerts.empty else 0

        score, rating, reasons = health_score(health.connected, alerts)
        if not health.connected:
            score_gauge(score, rating, reasons)
            ui.show_unavailable(health.error, health.error_detail, health.collected_at_local, t)
        else:
            srv = health.server
            dbs = health.databases
            db_sum = summarize_databases(dbs)
            sessions = ui.sessions_of(t.server)
            sess = summarize_sessions(sessions.sessions) if sessions.connected else {}
            backups = ui.backups_of(t.server)
            bk = (summarize_backups(evaluate_backups(backups.databases,
                                                     BackupPolicy.from_settings(settings)))
                  if backups.connected else {})
            cpu = ui.cpu_of(t.server)
            st.markdown(f"SQL Server name **`{srv.get('server_name')}`** · **{version_label(srv)}** · "
                        f"{srv.get('edition')} · build {srv.get('product_version')} · "
                        f"up {format_uptime(srv.get('uptime_seconds'))}")

            left, right = st.columns([1, 2.4], vertical_alignment="top")
            with left:
                score_gauge(score, rating, reasons)
            with right:
                log_df = dbs.dropna(subset=["log_used_pct"]) if not dbs.empty else dbs
                top_log = log_df.sort_values("log_used_pct").iloc[-1] if not log_df.empty else None
                cpu_series = cpu.points["sql_cpu_pct"].astype(float).tolist() if cpu.connected else None
                with st.container(horizontal=True, wrap=True, gap="small"):
                    card("Blocked sessions", sess.get("blocked", 0), "views/sessions_blocking.py",
                         "See blocking", help=f"{sess.get('sessions', 0)} user sessions")
                    card("SQL Server CPU", f"{cpu_series[-1]:.0f} %" if cpu_series else "n/a",
                         "views/query_performance.py", "Top queries", chart=cpu_series,
                         help="Last 60 minutes, from SQL Server's own per-minute history")
                    card("Highest log use", f"{top_log['log_used_pct']:.0f} %" if top_log is not None else "n/a",
                         "views/health.py", "Databases",
                         help=f"{top_log['database_name']}" if top_log is not None else None)
                    card("DBs online", f"{db_sum['online']} / {db_sum['total']}", "views/health.py",
                         "Databases", help=format_size_mb(db_sum["total_size_mb"]) + " in total")
                    card("Backup issues", bk.get("critical", 0) + bk.get("warning", 0), "views/backups.py",
                         "Backups", help=f"{bk.get('critical', 0)} critical · {bk.get('warning', 0)} overdue")
                    card("Alerts", f"{crit} ⛔ · {warn} ⚠️", "views/alerts.py", "Alerts")

            if cpu.connected and not cpu.points.empty:
                cpu_trend(cpu.points)

        st.markdown("**Open for this server**")
        with st.container(horizontal=True, wrap=True, gap="small"):
            st.page_link("views/health.py", label="Server health", icon=":material/monitor_heart:")
            st.page_link("views/query_performance.py", label="Query performance", icon=":material/bolt:")
            st.page_link("views/sessions_blocking.py", label="Sessions & blocking", icon=":material/lock_clock:")
            st.page_link("views/backups.py", label="Backups", icon=":material/backup:")
            st.page_link("views/alerts.py", label="Alerts & advice", icon=":material/notifications:")
            st.page_link("views/history.py", label="Performance history", icon=":material/insights:")


all_targets = ui.targets()
matches = search_servers(all_targets, query)
# Open a server directly when the query names it exactly (e.g. its real SQL Server
# name 'SQLHOST-01'), or when it is the only match.
single = exact_server_match(all_targets, query) or (matches[0] if len(matches) == 1 else None)

if query.strip():
    if not matches:
        st.warning(f"No server matches **{html.escape(query.strip())}**. Check the spelling or try "
                   "part of the name (friendly name, connection name, instance or computer name).",
                   icon="🔎")
    elif single is not None:
        if ui.current_target() != single:
            ui.select_server(single.name)
            st.rerun()          # redraw so the sidebar shows the newly chosen server too
        show_server_summary(single)
    else:
        st.caption(f"{len(matches)} servers match - choose one"
                   + (f" (showing the first {MAX_RESULTS}; type more to narrow it down)"
                      if len(matches) > MAX_RESULTS else ""))
        for t in matches[:MAX_RESULTS]:
            with st.container(border=True, horizontal=True, vertical_alignment="center"):
                st.markdown(f"**{html.escape(t.name)}** &nbsp; <span class='footnote'>"
                            f"{html.escape(t.server)}</span>", unsafe_allow_html=True)
                st.button("Select", key=f"pick_{t.name}", icon="➡️",
                          on_click=ui.select_server, args=(t.name,))
        chosen = ui.current_target()
        if chosen in matches:
            st.divider()
            show_server_summary(chosen)
else:
    chosen = ui.current_target()
    if chosen is None:
        st.info("Type a server name above to see its status. Only the server you choose is shown.",
                icon="🔎")
    else:
        show_server_summary(chosen)
        if len(all_targets) > 1:
            st.button("Clear selection", icon="✖️", on_click=ui.clear_server)

source = "fictional demo servers" if ui.demo_mode() else "server(s) in SQL_SERVERS (.env)"
ui.footnote(f"Search covers the {len(all_targets)} {source} by friendly name, "
            "connection name, instance name or real server (computer) name. Searching only reads the "
            "server list; SQL Server is queried only for the server you choose.")
