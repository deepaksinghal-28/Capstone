"""
Module 3 - Blocking and Active Session Monitoring.

Shows every user session, who is blocking whom (with the full chain back to the
head blocker), wait types and durations, and the current / last SQL statement.
Sources: sys.dm_exec_sessions, sys.dm_exec_requests, sys.dm_os_waiting_tasks,
         sys.dm_exec_connections, sys.dm_exec_sql_text

This page NEVER kills sessions. Ending a head blocker is a manual DBA decision.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from monitor import ui
from monitor.collectors import summarize_sessions
from monitor.config import get_settings

settings = get_settings()

ui.page_header("Sessions & Blocking")
target = ui.require_server()

auto = st.toggle("Auto-refresh every 10 seconds", value=False,
                 help="Re-reads the session DMVs every 10 s without reloading the whole page.")


def fmt_seconds(s: float) -> str:
    if s is None or pd.isna(s):
        return "–"
    s = float(s)
    if s >= 3600:
        return f"{s / 3600:.1f} h"
    if s >= 60:
        return f"{s / 60:.1f} min"
    return f"{s:.1f} s"


@st.fragment(run_every=10 if auto else None)
def live_view() -> None:
    snap = ui.sessions_of(target.server)
    if not snap.connected:
        ui.show_unavailable(snap.error, snap.error_detail, snap.collected_at_local)
        return

    if snap.requests_skipped:
        st.warning(
            "**Reduced detail:** database(s) "
            f"{', '.join(snap.recovering_databases)} are recovering/restoring. "
            "`sys.dm_exec_requests` is skipped until recovery finishes (SQL Server 2022 CU27 "
            "known issue KB5126934: querying it during recovery can cause an access violation). "
            "Blocking is still detected from `sys.dm_os_waiting_tasks`.",
            icon="🛡️",
        )

    sessions = snap.sessions
    chains = snap.chains
    summary = summarize_sessions(sessions)

    # -------------------------------------------------------------------------
    # Blocking banner - the most important thing on the page
    # -------------------------------------------------------------------------
    if summary["blocked"]:
        heads = sessions[sessions["is_head_blocker"]]
        head_text = "; ".join(
            f"session **{int(h.session_id)}** ({h.login_name}, {h.program_name or 'no program name'}, "
            f"{'idle with open transaction' if h.state != 'running' and h.open_transaction_count else h.session_status})"
            for h in heads.itertuples())
        prolonged = summary["longest_wait_s"] >= settings.blocking_warning_seconds
        message = (f"**Blocking detected:** {summary['blocked']} session(s) blocked, longest wait "
                   f"**{fmt_seconds(summary['longest_wait_s'])}**"
                   f"{' (prolonged: over ' + str(settings.blocking_warning_seconds) + ' s)' if prolonged else ''}. "
                   f"Head blocker: {head_text}.")
        st.error(message, icon="⛔")
    else:
        st.success("No blocking detected at this moment.", icon="✅")

    # -------------------------------------------------------------------------
    # Summary cards
    # -------------------------------------------------------------------------
    W = ui.CARD_WIDTH
    with st.container(horizontal=True, wrap=True, gap="small"):
        st.metric("User sessions", summary["sessions"], border=True, width=W,
                  help="Excludes this dashboard's own connections")
        st.metric("Running requests", summary["running"], border=True, width=W)
        st.metric("Blocked", summary["blocked"], border=True, width=W)
        st.metric("Head blockers", summary["head_blockers"], border=True, width=W)
        st.metric("Longest block wait", fmt_seconds(summary["longest_wait_s"]) if summary["blocked"] else "–",
                  border=True, width=W)
        st.metric("Open trans.", summary["open_tran"], border=True, width=W,
                  help="Sessions holding an open transaction (they may hold locks)")

    # -------------------------------------------------------------------------
    # Blocking chains
    # -------------------------------------------------------------------------
    if not chains.empty:
        st.subheader("Blocking chains")
        st.caption("Read a chain left to right: the first session blocks the next one, and so on. "
                   "Fix the HEAD blocker (first number) and the whole chain clears.")
        st.dataframe(
            chains, hide_index=True,
            column_order=["chain", "session_id", "blocked_by", "head_blocker", "wait_type",
                          "wait_seconds", "wait_resource", "database_name", "login_name", "sql_text"],
            column_config={
                "chain": st.column_config.TextColumn("Chain (head → … → blocked)"),
                "session_id": st.column_config.NumberColumn("Blocked session", format="%d"),
                "blocked_by": st.column_config.NumberColumn("Blocked by", format="%d"),
                "head_blocker": st.column_config.NumberColumn("Head blocker", format="%d"),
                "wait_type": st.column_config.TextColumn("Wait type"),
                "wait_seconds": st.column_config.NumberColumn("Waiting (s)", format="%.1f"),
                "wait_resource": st.column_config.TextColumn("Wait resource"),
                "database_name": st.column_config.TextColumn("Database"),
                "login_name": st.column_config.TextColumn("Login"),
                "sql_text": st.column_config.TextColumn("Blocked statement", width="large"),
            },
        )
        with st.expander("How to investigate and release blocking (DBA checklist)"):
            head_ids = ", ".join(str(int(s)) for s in sessions.loc[sessions["is_head_blocker"], "session_id"])
            st.markdown(
                f"1. Look at the **head blocker** ({head_ids}): its last statement is in the table below.\n"
                "2. *Idle with open transaction* usually means an application or user began a "
                "transaction and never committed - contact the owner (login / host / program).\n"
                "3. Check what it holds: `SELECT * FROM sys.dm_tran_locks WHERE request_session_id = <id>;`\n"
                "4. If the business owner agrees, the DBA may end it manually in SSMS with "
                "`KILL <id>;` - its work is **rolled back**, which can itself take time.\n"
                "5. Long term: shorter transactions, proper indexes, or READ_COMMITTED_SNAPSHOT.\n\n"
                "**This dashboard never kills sessions automatically.**"
            )

    # -------------------------------------------------------------------------
    # Current waits by type (only sessions that are waiting right now)
    # -------------------------------------------------------------------------
    waiting = sessions[(~sessions["is_monitor"]) & sessions["wait_type"].notna()
                       & (sessions["wait_seconds"] > 0)]
    if not waiting.empty:
        by_type = (waiting.groupby("wait_type")["wait_seconds"].agg(["count", "sum"])
                   .reset_index().sort_values("sum"))
        with st.container(border=True):
            st.markdown("**Current waits by wait type (seconds waited, summed over sessions)**")
            fig = go.Figure()
            fig.add_bar(y=by_type["wait_type"], x=by_type["sum"], orientation="h",
                        marker=dict(color=ui.SERIES_1, cornerradius=4), showlegend=False,
                        customdata=by_type["count"],
                        hovertemplate="<b>%{y}</b><br>%{x:,.1f} s waited<br>%{customdata} session(s)"
                                      "<extra></extra>")
            fig.update_xaxes(ticksuffix=" s")
            st.plotly_chart(ui.style_figure(fig, 80 + 36 * len(by_type), left_margin=160),
                            theme=None, config={"displayModeBar": False})

    # -------------------------------------------------------------------------
    # All sessions
    # -------------------------------------------------------------------------
    st.subheader("All user sessions")
    f1, f2, f3 = st.columns([2, 1, 1])
    search = f1.text_input("Search sessions", placeholder="Login, program, database or SQL…",
                           label_visibility="collapsed", key="sess_search")
    show_sleeping = f2.toggle("Show sleeping sessions", value=True, key="sess_sleeping")
    hide_monitor = f3.toggle("Hide this dashboard", value=True, key="sess_hide_monitor")

    table = sessions.copy()
    if hide_monitor:
        table = table[~table["is_monitor"]]
    if not show_sleeping:
        table = table[table["state"] != "sleeping"]
    if search:
        hay = (table["login_name"].fillna("") + " " + table["program_name"].fillna("") + " "
               + table["database_name"].fillna("") + " " + table["sql_text"])
        table = table[hay.str.contains(search, case=False, regex=False)]

    if table.empty:
        st.info("No sessions match the current filters.", icon="🔎")
        selected = []
    else:
        order = {"head_blocker": 0, "blocked_blocker": 1, "blocked": 2, "running": 3,
                 "idle_open_tran": 4, "sleeping": 5}
        table = table.assign(_o=table["state"].map(order)).sort_values(["_o", "session_id"])
        event = st.dataframe(
            table, hide_index=True, on_select="rerun", selection_mode="single-row",
            key="sess_table",
            column_order=["state_label", "session_id", "blocking_session_id", "login_name",
                          "program_name", "host_name", "database_name", "session_status",
                          "command", "wait_type", "wait_seconds", "elapsed_seconds", "cpu_ms",
                          "logical_reads", "open_transaction_count", "short_sql"],
            column_config={
                "state_label": st.column_config.TextColumn("State"),
                "session_id": st.column_config.NumberColumn("Session", format="%d"),
                "blocking_session_id": st.column_config.NumberColumn("Blocked by", format="%d"),
                "login_name": st.column_config.TextColumn("Login"),
                "program_name": st.column_config.TextColumn("Program"),
                "host_name": st.column_config.TextColumn("Host"),
                "database_name": st.column_config.TextColumn("Database"),
                "session_status": st.column_config.TextColumn("Status"),
                "command": st.column_config.TextColumn("Command"),
                "wait_type": st.column_config.TextColumn("Wait type"),
                "wait_seconds": st.column_config.NumberColumn("Wait (s)", format="%.1f"),
                "elapsed_seconds": st.column_config.NumberColumn("Running (s)", format="%.1f"),
                "cpu_ms": st.column_config.NumberColumn("CPU (ms)", format="localized"),
                "logical_reads": st.column_config.NumberColumn("Reads", format="localized"),
                "open_transaction_count": st.column_config.NumberColumn("Open tran", format="%d"),
                "short_sql": st.column_config.TextColumn("Current / last SQL", width="large"),
            },
        )
        selected = event.selection.rows if event and event.selection else []

    if selected:
        row = table.iloc[selected[0]]
        with st.container(border=True):
            st.markdown(f"**Session {int(row['session_id'])}** · {row['login_name']} · "
                        f"{row['program_name'] or 'no program name'} · {row['state_label']}")
            st.code(row["sql_text"] or "-- no SQL text available", language="sql")
    else:
        ui.footnote("Tip: click a row to see the session's complete current / last SQL statement.")

    ui.footnote(
        f"Live snapshot taken {snap.collected_at_local:%Y-%m-%d %H:%M:%S} (this PC)"
        f"{' · auto-refresh on' if auto else ''} · Sources: sys.dm_exec_sessions, "
        f"{'' if snap.requests_skipped else 'sys.dm_exec_requests, '}sys.dm_os_waiting_tasks, "
        "sys.dm_exec_connections, sys.dm_exec_sql_text · Prolonged-blocking threshold: "
        f"{settings.blocking_warning_seconds} s"
    )


live_view()
