"""
Module 1 - SQL Server Health Monitoring.

Shows: connectivity, instance identity, version/edition, uptime, CPU and memory,
every database with state, recovery model, sizes and transaction-log usage.
Sources: sys.dm_os_sys_info, sys.dm_os_process_memory, sys.configurations,
         sys.databases, sys.master_files, sys.dm_os_performance_counters
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from monitor import ui
from monitor.collectors import format_size_mb, format_uptime, summarize_databases, version_label
from monitor.config import get_settings

settings = get_settings()
ui.page_header("SQL Server Health")
target = ui.require_server()
snap = ui.health_of(target.server)

if not snap.connected:
    ui.show_unavailable(snap.error, snap.error_detail, snap.collected_at_local)
    st.stop()

srv = snap.server
dbs = snap.databases
summary = summarize_databases(dbs)

# ---------------------------------------------------------------------------
# Overall status banner
# ---------------------------------------------------------------------------
not_online = dbs[dbs["state_desc"] != "ONLINE"]
warnings = dbs[dbs["health_status"] == "Warning"]
if not not_online.empty:
    names = ", ".join(f"{r.database_name} ({r.state_desc})" for r in not_online.itertuples())
    st.error(f"**{len(not_online)} database(s) not ONLINE:** {names}", icon="⛔")
elif not warnings.empty:
    st.warning(f"**{len(warnings)} database(s) need attention:** "
               + ", ".join(warnings["database_name"]), icon="⚠️")
else:
    st.success(f"All {summary['total']} databases are ONLINE and healthy.", icon="✅")

# ---------------------------------------------------------------------------
# Instance identity (text facts) as a badge strip
# ---------------------------------------------------------------------------
offset = int(srv.get("server_utc_offset_minutes") or 0)
with st.container(horizontal=True, gap="small"):
    st.badge(str(srv.get("server_name", "n/a")), icon="🖥️", color="gray",
             help=f"Machine: {srv.get('machine_name')}")
    st.badge(version_label(srv), icon="🏷️", color="blue",
             help=str(srv.get("version_string", "")).splitlines()[0])
    st.badge(f"Build {srv.get('product_version')} · {srv.get('product_update_reference') or ''}",
             color="gray")
    st.badge(str(srv.get("edition", "n/a")), color="gray")
    st.badge(f"Server clock {'UTC' + format(offset / 60, '+g') if offset else 'UTC'}", icon="🕒",
             color="gray", help="SQL Server's own time zone. All ages are calculated with this clock.")

# ---------------------------------------------------------------------------
# Summary cards (numbers only)
# ---------------------------------------------------------------------------
in_use, max_mem = srv.get("sql_memory_in_use_mb"), srv.get("max_server_memory_mb")
W = ui.CARD_WIDTH
with st.container(horizontal=True, wrap=True, gap="small"):
    st.metric("Uptime", format_uptime(srv.get("uptime_seconds")), border=True, width=W,
              help=f"Started {srv.get('sqlserver_start_time'):%Y-%m-%d %H:%M} (server clock)")
    st.metric("DBs online", f"{summary['online']} / {summary['total']}", border=True, width=W,
              help=f"{summary['user']} user · {summary['system']} system")
    st.metric("Total size", format_size_mb(summary["total_size_mb"]), border=True, width=W,
              help="Sum of all data and log files (sys.master_files)")
    st.metric("SQL memory", format_size_mb(in_use), border=True, width=W,
              help=f"In use by SQL Server now · max server memory = {format_size_mb(max_mem)}")
    st.metric("Physical memory", format_size_mb(srv.get("physical_memory_mb")), border=True,
              width=W)
    st.metric("Logical CPUs", srv.get("cpu_count", "n/a"), border=True, width=W)

# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
left, right = st.columns(2)

chart_df = dbs.sort_values("total_size_mb", ascending=True)
with left:
    with st.container(border=True):
        st.markdown("**Database size by file type (MB)**")
        size_fig = go.Figure()
        for col, name, colour in [("data_size_mb", "Data files", ui.SERIES_1),
                                  ("log_size_mb", "Log files", ui.SERIES_2)]:
            size_fig.add_bar(
                y=chart_df["database_name"], x=chart_df[col], name=name, orientation="h",
                marker=dict(color=colour, line=dict(color=ui.SURFACE, width=2), cornerradius=4),
                hovertemplate="<b>%{y}</b><br>" + name + ": %{x:,.1f} MB<extra></extra>",
            )
        size_fig.update_layout(barmode="stack")
        size_fig.update_xaxes(ticksuffix=" MB")
        st.plotly_chart(ui.style_figure(size_fig, 300), theme=None,
                        config={"displayModeBar": False})

with right:
    with st.container(border=True):
        st.markdown("**Transaction log used (%)**")
        log_df = dbs.dropna(subset=["log_used_pct"]).sort_values("log_used_pct")
        log_fig = go.Figure()
        log_fig.add_bar(
            y=log_df["database_name"], x=log_df["log_used_pct"], orientation="h",
            marker=dict(color=ui.SERIES_1, cornerradius=4), showlegend=False,
            text=log_df["log_used_pct"].map(lambda v: f"{v:.0f}%"), textposition="outside",
            textfont=dict(color=ui.INK_2),
            hovertemplate="<b>%{y}</b><br>Log used: %{x:.0f}%<extra></extra>",
        )
        threshold = settings.log_used_warning_pct
        log_fig.add_vline(x=threshold, line=dict(color=ui.MUTED, width=2, dash="dash"),
                          annotation_text=f"Warning at {threshold}%",
                          annotation_position="top", annotation_font_color=ui.INK_2)
        log_fig.update_xaxes(range=[0, 105], ticksuffix="%")
        st.plotly_chart(ui.style_figure(log_fig, 300), theme=None,
                        config={"displayModeBar": False})

# ---------------------------------------------------------------------------
# Database table with search / filters
# ---------------------------------------------------------------------------
st.subheader("Databases")
f1, f2, f3 = st.columns([2, 1, 1])
search = f1.text_input("Search database", placeholder="Type part of a name…",
                       label_visibility="collapsed")
status_filter = f2.selectbox("Status", ["All statuses", "Healthy", "Warning", "Critical"],
                             label_visibility="collapsed", key="health_status")
show_system = f3.toggle("Show system databases", value=True)

table = dbs.copy()
if search:
    table = table[table["database_name"].str.contains(search, case=False, regex=False)]
if status_filter != "All statuses":
    table = table[table["health_status"] == status_filter]
if not show_system:
    table = table[table["is_system"] == 0]
table["status"] = table["health_status"].map(ui.STATUS_LABEL)
table["type"] = table["is_system"].map({1: "System", 0: "User"})

if table.empty:
    st.info("No databases match the current filters.", icon="🔎")
else:
    st.dataframe(
        table,
        hide_index=True,
        column_order=["status", "database_name", "state_desc", "type", "recovery_model_desc",
                      "user_access_desc", "compatibility_level", "data_size_mb",
                      "log_size_mb", "total_size_mb", "log_used_pct", "create_date"],
        column_config={
            "status": st.column_config.TextColumn("Health"),
            "database_name": st.column_config.TextColumn("Database"),
            "state_desc": st.column_config.TextColumn("State"),
            "type": st.column_config.TextColumn("Type"),
            "recovery_model_desc": st.column_config.TextColumn("Recovery model"),
            "user_access_desc": st.column_config.TextColumn("Access"),
            "compatibility_level": st.column_config.NumberColumn("Compat level", format="%d"),
            "data_size_mb": st.column_config.NumberColumn("Data (MB)", format="%.1f"),
            "log_size_mb": st.column_config.NumberColumn("Log (MB)", format="%.1f"),
            "total_size_mb": st.column_config.NumberColumn("Total (MB)", format="%.1f"),
            "log_used_pct": st.column_config.ProgressColumn("Log used", min_value=0,
                                                            max_value=100, format="%d%%"),
            "create_date": st.column_config.DatetimeColumn("Created", format="YYYY-MM-DD HH:mm"),
        },
    )

# ---------------------------------------------------------------------------
# Footer: last collection time + data sources
# ---------------------------------------------------------------------------
ui.footnote(
    f"Last collected {snap.collected_at_local:%Y-%m-%d %H:%M:%S} (this PC) · "
    f"{pd.Timestamp(snap.collected_at_server):%H:%M:%S} (SQL Server clock) · "
    "Sources: sys.dm_os_sys_info, sys.dm_os_process_memory, sys.configurations, "
    "sys.databases, sys.master_files, sys.dm_os_performance_counters"
)
