"""
Module 1 - SQL Server Health Monitoring.

Shows: connectivity, instance identity, version/edition, uptime, CPU and memory,
every database with state, recovery model, sizes and transaction-log usage.
Sources: sys.dm_os_sys_info, sys.dm_os_process_memory, sys.configurations,
         sys.databases, sys.master_files, sys.dm_os_performance_counters
"""

import altair as alt
import pandas as pd
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

# Clickable Altair charts: click a bar to select that database (double-click clears).
chart_df = dbs.sort_values("total_size_mb", ascending=False)
order = chart_df["database_name"].tolist()
with left:
    with st.container(border=True):
        st.markdown("**Database size by file type (MB)**")
        size_pick = alt.selection_point(name="pick", fields=["database_name"], on="click", clear="dblclick")
        long =chart_df.melt(id_vars=["database_name"], value_vars=["data_size_mb", "log_size_mb"],
                             var_name="file", value_name="mb")
        long["file"] = long["file"].map({"data_size_mb": "Data files", "log_size_mb": "Log files"})
        size_chart = alt.Chart(long).mark_bar(cornerRadiusEnd=3, cursor="pointer").encode(
            y=alt.Y("database_name:N", sort=order, title=None),
            x=alt.X("mb:Q", title="MB", stack="zero"),
            color=alt.Color("file:N", scale=alt.Scale(domain=["Data files", "Log files"],
                                                      range=[ui.SERIES_1, ui.SERIES_2])),
            opacity=alt.condition(size_pick, alt.value(1.0), alt.value(0.35)),
            tooltip=[alt.Tooltip("database_name:N", title="Database"), alt.Tooltip("file:N", title="File"),
                     alt.Tooltip("mb:Q", title="MB", format=",.1f")],
        ).add_params(size_pick)
        size_event = st.altair_chart(ui.style_altair(size_chart, 280), theme=None, key="size_chart",
                                     on_select="rerun", selection_mode="pick")

with right:
    with st.container(border=True):
        st.markdown("**Transaction log used (%)**")
        log_df = dbs.dropna(subset=["log_used_pct"])
        threshold = settings.log_used_warning_pct
        pick2 = alt.selection_point(name="pick", fields=["database_name"], on="click", clear="dblclick")
        y = alt.Y("database_name:N", sort="-x", title=None)
        bars = alt.Chart(log_df).mark_bar(cornerRadiusEnd=3, color=ui.SERIES_1, cursor="pointer").encode(
            y=y, x=alt.X("log_used_pct:Q", title="% of log file in use", scale=alt.Scale(domain=[0, 105], nice=False)),
            opacity=alt.condition(pick2, alt.value(1.0), alt.value(0.35)),
            tooltip=[alt.Tooltip("database_name:N", title="Database"),
                     alt.Tooltip("log_used_pct:Q", title="Log used %", format=".0f")],
        ).add_params(pick2)
        labels = alt.Chart(log_df).mark_text(align="left", dx=4, color=ui.INK_2).encode(
            y=y, x=alt.X("log_used_pct:Q", title="% of log file in use"),
            text=alt.Text("log_used_pct:Q", format=".0f"))
        rule = alt.Chart(pd.DataFrame({"t": [threshold]})).mark_rule(
            color=ui.MUTED, strokeDash=[5, 4], size=2).encode(x=alt.X("t:Q", title="% of log file in use"))
        log_event = st.altair_chart(ui.style_altair(alt.layer(bars, labels, rule), 280), theme=None,
                                    key="log_chart", on_select="rerun", selection_mode="pick")
        st.caption(f"Dashed line = warning at {threshold}% used.")


# ---------------------------------------------------------------------------
# Drill-down: click a bar in either chart (or pick from the list) -> details
# ---------------------------------------------------------------------------
names = dbs["database_name"].tolist()
# Each chart keeps its own selection, so remember each one separately: whichever
# chart was clicked most recently (a NEW value) wins over the other and the list.
for chart_name, chart_event in (("size", size_event), ("log", log_event)):
    clicked = ui.altair_pick(chart_event, "database_name")
    last_key = f"_health_last_{chart_name}"
    if clicked in names and clicked != st.session_state.get(last_key):
        st.session_state["health_db_pick"] = clicked
    st.session_state[last_key] = clicked
st.caption("Tip: click a bar in either chart to open that database's details.")
pick = st.selectbox("Database details", ["-"] + names, key="health_db_pick",
                    format_func=lambda n: "Choose a database…" if n == "-" else n)
if pick and pick != "-":
    row = dbs.set_index("database_name").loc[pick]
    backups = ui.backups_of(target.server)
    bk_row = None
    if backups.connected and not backups.databases.empty:
        from monitor.collectors import BackupPolicy, evaluate_backups
        ev = evaluate_backups(backups.databases, BackupPolicy.from_settings(settings))
        match = ev[ev["database_name"] == pick]
        bk_row = match.iloc[0] if not match.empty else None
    with st.container(border=True):
        st.markdown(f"**{pick}** · {ui.STATUS_LABEL.get(row['health_status'], row['health_status'])} · "
                    f"{row['state_desc']} · {row['recovery_model_desc']} recovery · "
                    f"compatibility {row['compatibility_level']}")
        W = ui.CARD_WIDTH
        with st.container(horizontal=True, wrap=True, gap="small"):
            st.metric("Data files", f"{row['data_size_mb']:,.1f} MB", border=True, width=W)
            st.metric("Log file", f"{row['log_size_mb']:,.1f} MB", border=True, width=W)
            st.metric("Log used", f"{row['log_used_pct']:.0f} %" if pd.notna(row["log_used_pct"]) else "n/a",
                      border=True, width=W)
            if bk_row is not None:
                st.metric("Last full backup", bk_row["full_age_text"] + (" ago" if bk_row["full_age_text"] != "never" else ""),
                          border=True, width=W)
                st.metric("Log backup", bk_row["log_backup_state"], border=True, width=W)
        if bk_row is not None:
            st.markdown(f"Backup check: {bk_row['findings']}")
            if bk_row.get("log_reuse_wait_desc") not in (None, "NOTHING"):
                st.markdown(f"The log can't be reused yet because of **{bk_row['log_reuse_wait_desc']}**.")
        st.page_link("views/backups.py", label="Open backup details", icon=":material/arrow_forward:")

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
