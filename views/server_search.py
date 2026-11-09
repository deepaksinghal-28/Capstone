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
import streamlit as st

from monitor import ui
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

        if not health.connected:
            ui.show_unavailable(health.error, health.error_detail, health.collected_at_local, t)
        else:
            srv = health.server
            db_sum = summarize_databases(health.databases)
            sessions = ui.sessions_of(t.server)
            sess = summarize_sessions(sessions.sessions) if sessions.connected else {}
            backups = ui.backups_of(t.server)
            bk = (summarize_backups(evaluate_backups(backups.databases,
                                                     BackupPolicy.from_settings(settings)))
                  if backups.connected else {})
            st.markdown(f"SQL Server name **`{srv.get('server_name')}`** · **{version_label(srv)}** · "
                        f"{srv.get('edition')} · build {srv.get('product_version')}")
            W = ui.CARD_WIDTH
            with st.container(horizontal=True, wrap=True, gap="small"):
                st.metric("Uptime", format_uptime(srv.get("uptime_seconds")), border=True, width=W)
                st.metric("DBs online", f"{db_sum['online']} / {db_sum['total']}", border=True, width=W)
                st.metric("Total size", format_size_mb(db_sum["total_size_mb"]), border=True, width=W)
                st.metric("Sessions", sess.get("sessions", 0), border=True, width=W)
                st.metric("Blocked", sess.get("blocked", 0), border=True, width=W)
                st.metric("Backup issues", bk.get("critical", 0) + bk.get("warning", 0), border=True,
                          width=W, help=f"{bk.get('critical', 0)} critical · {bk.get('warning', 0)} overdue")
                st.metric("Alerts", f"{crit} ⛔ · {warn} ⚠️", border=True, width=W)

        st.markdown("**Open for this server**")
        with st.container(horizontal=True, wrap=True, gap="small"):
            st.page_link("views/health.py", label="Server health", icon="🩺")
            st.page_link("views/query_performance.py", label="Query performance", icon="⚡")
            st.page_link("views/sessions_blocking.py", label="Sessions & blocking", icon="🔒")
            st.page_link("views/backups.py", label="Backups", icon="💾")
            st.page_link("views/alerts.py", label="Alerts & advice", icon="🔔")


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
