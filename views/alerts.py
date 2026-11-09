"""
Module 5 - Alerts and DBA Recommendations.

Rule-based alerts built from the data of Modules 1-4 (plus low-memory
notifications). Each alert shows name, severity, affected server/database,
description, detection time and the recommended DBA investigation steps.
Refreshing does not create duplicates: alerts are tracked by a stable key in a
local SQLite file (see monitor/alerts.py).
"""

import os

import pandas as pd
import streamlit as st

from monitor import ui
from monitor.alerts import RULES, age_text, default_store_path, summarize_alerts
from monitor.config import get_settings

settings = get_settings()
ui.page_header("Alerts & DBA Recommendations")

run = ui.evaluate_alerts()
if run.get("store_error"):
    st.error(run["store_error"], icon="⛔")
    st.stop()

store = ui.alert_store()
active = store.frame("ACTIVE")
resolved = store.frame("RESOLVED")

# Only the chosen server's alerts by default (rules still run for every server).
target = ui.current_target()
multi = len(ui.targets()) > 1
all_servers = multi and st.toggle("Show alerts from all servers", value=False, key="al_all",
                                  help="Rules run for every server in SQL_SERVERS on each check; "
                                       "by default only the chosen server is shown.")
if multi and not all_servers:
    if target is None:
        ui.require_server()                      # stops with a "search first" hint
    if not active.empty:
        active = active[active["server"] == target.name]
    if not resolved.empty:
        resolved = resolved[resolved["server"] == target.name]
summary = summarize_alerts(active)

# ---------------------------------------------------------------------------
# Banner + cards
# ---------------------------------------------------------------------------
if summary["critical"]:
    st.error(f"**{summary['critical']} critical** and {summary['warning']} warning alert(s) need attention.",
             icon="⛔")
elif summary["warning"]:
    st.warning(f"**{summary['warning']} warning alert(s)** need attention.", icon="⚠️")
else:
    st.success("No active alerts - every rule is within its threshold.", icon="✅")

resolved_24h = (resolved[resolved["resolved_at"] >= pd.Timestamp.now() - pd.Timedelta(hours=24)]
                if not resolved.empty else resolved)
W = ui.CARD_WIDTH
with st.container(horizontal=True, wrap=True, gap="small"):
    st.metric("Active", summary["active"], border=True, width=W)
    st.metric("Critical", summary["critical"], border=True, width=W)
    st.metric("Warning", summary["warning"], border=True, width=W)
    st.metric("Resolved (24 h)", len(resolved_24h), border=True, width=W)
    st.metric("Last check", f"{run['evaluated_at']:%H:%M:%S}", border=True, width=W,
              help=f"Rules run at most every {settings.cache_seconds} s (this PC's clock). "
                   f"This check: {run.get('opened', 0)} new, {run.get('updated', 0)} still active, "
                   f"{run.get('reopened', 0)} re-opened, {run.get('resolved', 0)} resolved.")

c1, c2 = st.columns([1, 3])
if c1.button("Check now", icon="🔄", help="Clear cached data and run every rule again"):
    st.cache_data.clear()
    st.rerun()
c2.caption("Alerts are de-duplicated: a refresh updates *last seen* and the check counter instead "
           "of creating a new alert. Cleared conditions move to *Resolved*.")

# ---------------------------------------------------------------------------
# Active alerts - one card each, most severe first
# ---------------------------------------------------------------------------
def who() -> str:
    """Who acknowledged - the Windows user locally, 'demo visitor' in the public demo."""
    return "demo visitor" if settings.demo_mode else (os.getenv("USERNAME") or "dashboard user")


def act(action: str, key: str) -> None:
    """Button callbacks: change ONLY the local alert file, never SQL Server."""
    if action == "ack":
        store.acknowledge(key, who())
    elif action == "unack":
        store.unacknowledge(key)
    elif action == "snooze":
        store.snooze(key, 60)
    elif action == "unsnooze":
        store.unsnooze(key)


st.subheader("Active alerts")
if active.empty:
    st.info("Nothing to investigate right now.", icon="🎉")
else:
    f1, f2 = st.columns([3, 1], vertical_alignment="bottom")
    sev_filter = f1.multiselect("Severity", ["Critical", "Warning", "Info"],
                                default=["Critical", "Warning", "Info"], key="al_sev")
    show_snoozed = f2.toggle("Show snoozed", value=False, key="al_show_snoozed")
    shown = active[active["severity"].isin(sev_filter)]
    snoozed_count = int(shown["is_snoozed"].sum())
    if not show_snoozed:
        shown = shown[~shown["is_snoozed"]]
    if snoozed_count and not show_snoozed:
        st.caption(f"{snoozed_count} snoozed alert(s) hidden - switch on *Show snoozed* to see them.")
    # Unacknowledged first, then by severity.
    shown = shown.sort_values(["is_acknowledged", "severity_rank", "first_seen"])
    for a in shown.itertuples():
        with st.container(border=True):
            top_left, top_right = st.columns([3, 2])
            server_part = f"{a.server} · " if isinstance(a.server, str) and a.server else ""
            state = []
            if a.is_acknowledged:
                state.append(f"✔ Acknowledged by {a.acknowledged_by} at {a.acknowledged_at:%H:%M}")
            if a.is_snoozed:
                state.append(f"💤 Snoozed until {a.snoozed_until:%H:%M}")
            top_left.markdown(f"**{a.severity_label} · {a.name}**  \n"
                              f"Affected: **{server_part}{a.target}**"
                              + (f"  \n<span class='footnote'>{' · '.join(state)}</span>" if state else ""),
                              unsafe_allow_html=True)
            top_right.markdown(
                f"<div class='footnote' style='text-align:right'>Detected {a.first_seen:%Y-%m-%d %H:%M:%S} "
                f"({age_text(a.first_seen)})<br>Last seen {a.last_seen:%H:%M:%S} · "
                f"seen in {a.checks_seen} check(s)"
                f"{' · opened ' + str(a.times_opened) + ' times' if a.times_opened > 1 else ''}</div>",
                unsafe_allow_html=True)
            st.markdown(a.description)
            with st.expander("Recommended DBA investigation steps"):
                st.markdown("\n".join(f"{i}. {step}" for i, step in
                                      enumerate(a.recommendation.split("\n"), start=1)))
            with st.container(horizontal=True, gap="small"):
                if a.is_acknowledged:
                    st.button("Undo acknowledge", key=f"unack_{a.alert_key}", on_click=act,
                              args=("unack", a.alert_key))
                else:
                    st.button("Acknowledge", icon="✔", key=f"ack_{a.alert_key}", on_click=act,
                              args=("ack", a.alert_key), help="Mark as seen and being handled")
                if a.is_snoozed:
                    st.button("Wake up now", key=f"unsnooze_{a.alert_key}", on_click=act,
                              args=("unsnooze", a.alert_key))
                else:
                    st.button("Snooze 1 h", icon="💤", key=f"snooze_{a.alert_key}", on_click=act,
                              args=("snooze", a.alert_key),
                              help="Hide this alert for 1 hour; it comes back if still active")

# ---------------------------------------------------------------------------
# Resolved alerts
# ---------------------------------------------------------------------------
st.subheader("Resolved alerts")
if resolved.empty:
    st.caption(f"No alerts resolved in the last {settings.alert_history_days} days.")
else:
    view = resolved.sort_values("resolved_at", ascending=False).assign(
        duration=lambda d: ((d["resolved_at"] - d["first_seen"]).dt.total_seconds() / 60)
        .map(lambda m: f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h"))
    st.dataframe(
        view, hide_index=True,
        column_order=["severity_label", "server", "name", "target", "first_seen", "resolved_at",
                      "duration", "checks_seen", "description"],
        column_config={
            "severity_label": st.column_config.TextColumn("Severity"),
            "server": st.column_config.TextColumn("Server"),
            "name": st.column_config.TextColumn("Alert"),
            "target": st.column_config.TextColumn("Affected"),
            "first_seen": st.column_config.DatetimeColumn("Detected", format="YYYY-MM-DD HH:mm:ss"),
            "resolved_at": st.column_config.DatetimeColumn("Resolved", format="YYYY-MM-DD HH:mm:ss"),
            "duration": st.column_config.TextColumn("Lasted"),
            "checks_seen": st.column_config.NumberColumn("Checks", format="%d"),
            "description": st.column_config.TextColumn("Last description", width="large"),
        },
    )

# ---------------------------------------------------------------------------
# Rule catalogue and thresholds
# ---------------------------------------------------------------------------
with st.expander("Alert rules and current thresholds"):
    st.dataframe(pd.DataFrame(RULES, columns=["Key", "Alert", "Severity", "Condition"]),
                 hide_index=True)
    st.markdown(
        f"Thresholds from `.env`: log used warning **{settings.log_used_warning_pct}%** / critical "
        f"**{settings.log_used_critical_pct}%** · prolonged blocking **{settings.blocking_warning_seconds} s** · "
        f"long-running **{settings.long_running_seconds} s** · backups: full **{settings.full_backup_max_hours} h**, "
        f"full/diff **{settings.data_backup_max_hours} h**, log **{settings.log_backup_max_minutes} min** · "
        f"memory look-back **{settings.memory_window_minutes} min**."
    )

ui.footnote(
    f"Alert store: {default_store_path(settings)} (local SQLite file, outside OneDrive) · "
    f"resolved alerts kept {settings.alert_history_days} days · times are this PC's clock · "
    "Rule-based only - no machine learning."
)
