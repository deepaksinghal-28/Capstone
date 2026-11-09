"""
Module 4 - Database Backup Monitoring.

For every database: last full / differential / log backup, backup age, recovery
model handling and configurable overdue warnings. Plus recent backup history.
Sources: msdb.dbo.backupset, msdb.dbo.backupmediafamily, sys.databases,
         sys.database_recovery_status

IMPORTANT: msdb records that a backup COMPLETED. It does not prove the file
still exists or can be restored - that needs RESTORE VERIFYONLY and test restores.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from monitor import ui
from monitor.collectors import BackupPolicy, evaluate_backups, format_age_minutes, summarize_backups
from monitor.config import get_settings

settings = get_settings()
ui.page_header("Backup Monitoring")
target = ui.require_server()

snap = ui.backups_of(target.server)
if not snap.connected:
    ui.show_unavailable(snap.error, snap.error_detail, snap.collected_at_local)
    st.stop()

# ---------------------------------------------------------------------------
# Backup policy - defaults from .env, adjustable here for this browser session only
# ---------------------------------------------------------------------------
defaults = BackupPolicy.from_settings(settings)
with st.expander(f"Backup policy · full every {defaults.full_max_hours} h · full/diff every "
                 f"{defaults.data_max_hours} h · log every {defaults.log_max_minutes} min "
                 "(click to adjust for this session)"):
    p1, p2, p3 = st.columns(3)
    full_h = p1.number_input("Full backup at least every (hours)", 1, 24 * 60,
                             defaults.full_max_hours, key="pol_full")
    data_h = p2.number_input("Full or differential at least every (hours)", 1, 24 * 60,
                             defaults.data_max_hours, key="pol_data")
    log_m = p3.number_input("Log backup at least every (minutes)", 1, 24 * 60,
                            defaults.log_max_minutes, key="pol_log")
    st.caption("Changes here apply only to this browser session (useful for demonstrating an "
               "overdue condition). Permanent values live in .env: ALERT_FULL_BACKUP_HOURS, "
               "ALERT_DATA_BACKUP_HOURS, ALERT_LOG_BACKUP_MINUTES.")
policy = BackupPolicy(int(full_h), int(data_h), int(log_m))
if policy != defaults:
    st.info(f"Using an adjusted policy for this session: full {policy.full_max_hours} h · "
            f"full/diff {policy.data_max_hours} h · log {policy.log_max_minutes} min.", icon="🎛️")

dbs = evaluate_backups(snap.databases, policy)
summary = summarize_backups(dbs)

st.warning(
    "**Backup history is not a verified restore.** These dates come from `msdb` and only prove a "
    "backup *completed*. They do not prove the backup file still exists, is undamaged, or can be "
    "restored. Verify with `RESTORE VERIFYONLY … WITH CHECKSUM` and, above all, regular test restores.",
    icon="📋",
)

# ---------------------------------------------------------------------------
# Status banner + cards
# ---------------------------------------------------------------------------
if summary["critical"]:
    st.error(f"**{summary['critical']} database(s) critical**, {summary['warning']} overdue: "
             + ", ".join(dbs.loc[dbs["backup_status"] == "Critical", "database_name"]), icon="⛔")
elif summary["warning"]:
    st.warning(f"**{summary['warning']} database(s) have overdue backups:** "
               + ", ".join(dbs.loc[dbs["backup_status"] == "Warning", "database_name"]), icon="⚠️")
else:
    st.success(f"All {summary['databases']} databases are within the backup policy.", icon="✅")

W = ui.CARD_WIDTH
with st.container(horizontal=True, wrap=True, gap="small"):
    st.metric("Databases", summary["databases"], border=True, width=W,
              help="tempdb is excluded: it is recreated at every restart and cannot be backed up")
    st.metric("Within policy", summary["healthy"], border=True, width=W)
    st.metric("Overdue", summary["warning"], border=True, width=W)
    st.metric("Critical", summary["critical"], border=True, width=W,
              help="No recorded full backup, or the last full backup is older than the policy")
    st.metric("Latest backup", format_age_minutes(summary["newest_age"]) +
              (" ago" if summary["newest_age"] is not None and pd.notna(summary["newest_age"]) else ""),
              border=True, width=W)

# ---------------------------------------------------------------------------
# Chart: hours since last full/differential, with the policy line
# ---------------------------------------------------------------------------
backed_up = dbs[dbs["data_age_minutes"].notna()].copy()
with st.container(border=True):
    st.markdown("**Hours since the last full or differential backup**")
    if backed_up.empty:
        st.info("No database has a recorded full backup yet, so there is nothing to plot. "
                "See the table below for the reason per database.", icon="ℹ️")
    else:
        backed_up["data_age_hours"] = backed_up["data_age_minutes"].astype(float) / 60
        backed_up = backed_up.sort_values("data_age_hours")
        fig = go.Figure()
        fig.add_bar(y=backed_up["database_name"], x=backed_up["data_age_hours"], orientation="h",
                    marker=dict(color=ui.SERIES_1, cornerradius=4), showlegend=False,
                    text=backed_up["data_age_minutes"].map(format_age_minutes),
                    textposition="outside", textfont=dict(color=ui.INK_2),
                    hovertemplate="<b>%{y}</b><br>%{x:.2f} h since last full/diff<extra></extra>")
        fig.add_vline(x=policy.data_max_hours, line=dict(color=ui.MUTED, width=2, dash="dash"),
                      annotation_text=f"Policy: {policy.data_max_hours} h",
                      annotation_position="top", annotation_font_color=ui.INK_2)
        x_max = max(policy.data_max_hours, float(backed_up["data_age_hours"].max())) * 1.15
        fig.update_xaxes(range=[0, x_max], ticksuffix=" h")
        st.plotly_chart(ui.style_figure(fig, 90 + 40 * len(backed_up), left_margin=120),
                        theme=None, config={"displayModeBar": False})

# ---------------------------------------------------------------------------
# Per-database table
# ---------------------------------------------------------------------------
st.subheader("Backup status by database")
f1, f2 = st.columns([1, 1])
status_filter = f1.selectbox("Status", ["All statuses", "Critical", "Warning", "Healthy"],
                             format_func=lambda s: {"Warning": "Overdue"}.get(s, s),
                             label_visibility="collapsed", key="bk_status")
show_system = f2.toggle("Show system databases", value=True, key="bk_system")
table = dbs.copy()
if status_filter != "All statuses":
    table = table[table["backup_status"] == status_filter]
if not show_system:
    table = table[table["is_system"] == 0]

if table.empty:
    st.info("No databases match the current filters.", icon="🔎")
else:
    order = {"Critical": 0, "Warning": 1, "Not checked": 2, "Healthy": 3}
    table = table.assign(_o=table["backup_status"].map(order)).sort_values(["_o", "database_name"])
    st.dataframe(
        table, hide_index=True,
        column_order=["status_label", "database_name", "recovery_model_desc", "last_full",
                      "full_age_text", "last_diff", "diff_age_text", "last_log",
                      "log_backup_state", "last_full_size_mb", "findings"],
        column_config={
            "status_label": st.column_config.TextColumn("Status"),
            "database_name": st.column_config.TextColumn("Database"),
            "recovery_model_desc": st.column_config.TextColumn("Recovery model"),
            "last_full": st.column_config.DatetimeColumn("Last full", format="YYYY-MM-DD HH:mm"),
            "full_age_text": st.column_config.TextColumn("Full age"),
            "last_diff": st.column_config.DatetimeColumn("Last differential", format="YYYY-MM-DD HH:mm"),
            "diff_age_text": st.column_config.TextColumn("Diff age"),
            "last_log": st.column_config.DatetimeColumn("Last log", format="YYYY-MM-DD HH:mm"),
            "log_backup_state": st.column_config.TextColumn("Log backup"),
            "last_full_size_mb": st.column_config.NumberColumn("Full size (MB)", format="%.1f"),
            "findings": st.column_config.TextColumn("Findings / reason", width="large"),
        },
    )
    ui.footnote("Backup dates are shown on the SQL Server clock (UTC on this PC). Ages are "
                "calculated by SQL Server, so they are always consistent.")

# ---------------------------------------------------------------------------
# Recent backup history
# ---------------------------------------------------------------------------
st.subheader("Recent backup history")
history = snap.history
if history.empty:
    st.info("msdb has no backup history for this server yet - no backup has ever been taken here.",
            icon="📭")
else:
    h1, h2 = st.columns(2)
    hist_db = h1.selectbox("History database", ["All databases"] + sorted(history["database_name"].unique()),
                           label_visibility="collapsed")
    hist_type = h2.selectbox("History type", ["All types", "Full", "Differential", "Log"],
                             label_visibility="collapsed")
    view = history
    if hist_db != "All databases":
        view = view[view["database_name"] == hist_db]
    if hist_type != "All types":
        view = view[view["backup_type"] == hist_type]
    view = view.assign(age_text=view["age_minutes"].map(format_age_minutes))
    st.dataframe(
        view, hide_index=True,
        column_order=["backup_finish_date", "database_name", "backup_type", "age_text",
                      "duration_seconds", "size_mb", "compressed_mb", "has_checksum",
                      "is_copy_only", "recovery_model", "user_name", "physical_device_name"],
        column_config={
            "backup_finish_date": st.column_config.DatetimeColumn("Finished", format="YYYY-MM-DD HH:mm:ss"),
            "database_name": st.column_config.TextColumn("Database"),
            "backup_type": st.column_config.TextColumn("Type"),
            "age_text": st.column_config.TextColumn("Age"),
            "duration_seconds": st.column_config.NumberColumn("Duration (s)", format="%d"),
            "size_mb": st.column_config.NumberColumn("Size (MB)", format="%.1f"),
            "compressed_mb": st.column_config.NumberColumn("Compressed (MB)", format="%.1f"),
            "has_checksum": st.column_config.CheckboxColumn("Checksum"),
            "is_copy_only": st.column_config.CheckboxColumn("Copy-only"),
            "recovery_model": st.column_config.TextColumn("Recovery model"),
            "user_name": st.column_config.TextColumn("Taken by"),
            "physical_device_name": st.column_config.TextColumn("Backup file", width="large"),
        },
    )

ui.footnote(
    f"Last collected {snap.collected_at_local:%Y-%m-%d %H:%M:%S} (this PC) · Sources: "
    "msdb.dbo.backupset, msdb.dbo.backupmediafamily, sys.databases, sys.database_recovery_status"
)
