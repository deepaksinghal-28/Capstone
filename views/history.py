"""
Module 6 - Performance History & Trend Analysis.

How ONE server has changed over time, from the snapshots the history collector
(tools/collect_history.py, started by run_app.ps1) saves every few minutes:
health score, CPU, the busiest hours of the week, sessions and blocking
incidents, database growth with a 30-day projection, and transaction-log use.

Source: history.db on this PC. This page never queries SQL Server itself.
"""

from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from monitor import ui
from monitor.config import get_settings
from monitor.history import (WEEKDAYS, blocking_incidents, busiest_hours, growth_table,
                             quietest_hour, resample, unreachable_minutes)

settings = get_settings()
ui.page_header("Performance History")
target = ui.require_server()
full = ui.history_of(target)
COLLECT_CMD = r'& "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" tools\collect_history.py'

if full.empty:
    st.info(
        f"No history has been recorded for **{target.name}** yet.\n\n"
        f"The history collector saves a snapshot of every monitored server every "
        f"{settings.history_interval_seconds // 60} minutes. It starts automatically with "
        "`run_app.ps1`, or run it yourself in the project folder:", icon="📈")
    st.code(COLLECT_CMD, language="powershell")
    st.caption("The first charts appear after the first snapshot; trends become meaningful after a few hours.")
    st.stop()


def fmt_minutes(minutes: float) -> str:
    if minutes >= 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes:.0f} min"


now = datetime.now()
stale_minutes = (now - full.last_snapshot).total_seconds() / 60 if full.last_snapshot else 0
if not full.sample and stale_minutes > 3 * full.interval_seconds / 60:
    st.warning(f"No new snapshot since **{full.last_snapshot:%Y-%m-%d %H:%M}** ({fmt_minutes(stale_minutes)} ago): "
               "the history collector is not running. Restart the dashboard with `run_app.ps1` "
               "(it starts the collector), or run it on its own:", icon="⏸️")
    st.code(COLLECT_CMD, language="powershell")

# ---------------------------------------------------------------------------
# Time range (one control, above everything it affects)
# ---------------------------------------------------------------------------
RANGES = {"24 hours": 1, "7 days": 7, "14 days": 14, "30 days": 30}
default_range = "7 days" if full.span_days >= 2 else "24 hours"
choice = st.segmented_control("Time range", list(RANGES), default=default_range, key="hist_range") \
    or default_range
days = RANGES[choice]
h = full.since(now - timedelta(days=days))
bucket = None if days <= 1 else "1h"            # hourly points beyond one day keep charts readable
sv = h.servers
MODE = "lines+markers" if len(sv) < 24 else "lines"   # a new history has too few points for a line
coverage = (f"{len(sv):,} snapshots, {sv['collected_at'].min():%d %b %H:%M} → "
            f"{sv['collected_at'].max():%d %b %H:%M} · one every {h.interval_seconds // 60} min · "
            "times are this PC's clock" + (" · charts show hourly values" if bucket else ""))
st.caption(coverage)
if full.first_snapshot and full.first_snapshot > now - timedelta(days=days) and not full.sample:
    st.caption(f"History starts {full.first_snapshot:%d %b %H:%M}, so this range is not fully covered yet.")

# ---------------------------------------------------------------------------
# Summary cards for the selected range
# ---------------------------------------------------------------------------
scores = pd.to_numeric(sv["health_score"], errors="coerce")
incidents = blocking_incidents(sv, h.interval_seconds)
cpu = h.cpu.assign(sql_cpu_pct=pd.to_numeric(h.cpu["sql_cpu_pct"], errors="coerce"))
growth = growth_table(h.databases)
user_growth = growth[(growth["is_system"] == 0) & growth["mb_per_day"].notna()]
down = unreachable_minutes(sv, h.interval_seconds)
sessions = pd.to_numeric(sv["user_sessions"], errors="coerce")

cards = st.columns(3) + st.columns(3)          # two tidy rows of three
cards[0].metric("Lowest health score", f"{scores.min():.0f}", border=True,
                help=f"Average {scores.mean():.1f} · now {scores.iloc[-1]:.0f}")
if not cpu.empty and cpu["sql_cpu_pct"].notna().any():
    peak = cpu.loc[cpu["sql_cpu_pct"].idxmax()]
    when = f"{peak['event_time']:%a %d %b %H:%M}" if peak["sql_cpu_pct"] > 0 else None
    cards[1].metric("Peak SQL CPU", f"{peak['sql_cpu_pct']:.0f} %", when,
                    delta_color="off", delta_arrow="off", border=True,
                    help=f"Average {cpu['sql_cpu_pct'].mean():.0f} % over the range")
else:
    cards[1].metric("Peak SQL CPU", "–", border=True)
cards[2].metric("Blocking incidents", len(incidents),
                f"{fmt_minutes(incidents['minutes'].sum())} in total" if len(incidents) else None,
                delta_color="off", delta_arrow="off", border=True,
                help="Consecutive snapshots with blocked sessions count as one incident")
cards[3].metric("Peak user sessions", f"{sessions.max():.0f}" if sessions.notna().any() else "–", border=True,
                help=f"Average {sessions.mean():.0f}" if sessions.notna().any() else None)
if not user_growth.empty:
    top = user_growth.iloc[0]
    cards[4].metric("Fastest-growing database", top["database_name"], f"{top['mb_per_day']:+,.0f} MB per day",
                    delta_color="off", delta_arrow="off", border=True,
                    help="Data + log size, trend line through every snapshot in the range")
else:
    cards[4].metric("Fastest-growing database", "–", border=True,
                    help="Needs at least an hour of history to measure growth")
cards[5].metric("Time unreachable", fmt_minutes(down), border=True,
                help="Snapshots where SQL Server could not be reached")


def time_axis(fig: go.Figure) -> go.Figure:
    """Day ticks for multi-day ranges, clock times for 24 hours; full date + time on hover."""
    fig.update_xaxes(tickformat="%a %d %b" if bucket else "%H:%M", hoverformat="%a %d %b %H:%M")
    return fig


def line_figure() -> go.Figure:
    fig = go.Figure()
    fig.update_layout(hovermode="x unified")
    return time_axis(fig)


# ---------------------------------------------------------------------------
# Health score and CPU
# ---------------------------------------------------------------------------
left, right = st.columns(2)
with left, st.container(border=True):
    st.markdown("**Health score**" + (" (lowest per hour)" if bucket else ""))
    hs = resample(sv, "collected_at", ["health_score"], bucket, how="min")
    fig = line_figure()
    fig.add_scatter(x=hs["collected_at"], y=hs["health_score"], mode=MODE, name="Health score",
                    line=dict(color=ui.SERIES_1, width=2), marker=dict(size=8),
                    hovertemplate="Score %{y:.0f}<extra></extra>")
    for level, label in ((85, "Good ≥ 85"), (60, "Fair ≥ 60")):
        fig.add_hline(y=level, line=dict(color=ui.MUTED, width=1, dash="dot"), annotation_text=label,
                      annotation_position="bottom left", annotation_font=dict(color=ui.INK_2, size=11))
    fig.update_yaxes(range=[0, 104])
    fig.update_layout(showlegend=False)
    st.plotly_chart(ui.style_figure(fig, 260, left_margin=40), theme=None, config={"displayModeBar": False})

with right, st.container(border=True):
    st.markdown("**CPU** - SQL Server and other processes" + (" (hourly average)" if bucket else ""))
    c = resample(cpu, "event_time", ["sql_cpu_pct", "other_cpu_pct"], bucket)
    fig = line_figure()
    for col, name, colour in (("sql_cpu_pct", "SQL Server", ui.SERIES_1),
                              ("other_cpu_pct", "Other processes", ui.MUTED)):
        fig.add_scatter(x=c["event_time"], y=c[col], name=name, mode="lines", stackgroup="cpu",
                        line=dict(width=1.5, color=colour), hovertemplate=name + " %{y:.0f} %<extra></extra>")
    fig.update_yaxes(range=[0, 100], ticksuffix=" %")
    st.plotly_chart(ui.style_figure(fig, 260, left_margin=50), theme=None, config={"displayModeBar": False})

# ---------------------------------------------------------------------------
# Busiest hours of the week (when to schedule maintenance)
# ---------------------------------------------------------------------------
with st.container(border=True):
    st.markdown("**Busiest hours** - average SQL Server CPU by day of week and hour")
    pivot = busiest_hours(cpu)
    covered_days = cpu["event_time"].dt.date.nunique() if not cpu.empty else 0
    if covered_days < 2:
        st.caption("Needs at least 2 days of history - this pattern shows when the server is usually "
                   "busy and the best time for maintenance.")
    else:
        steps = len(ui.BLUE_RAMP) - 1
        fig = go.Figure(go.Heatmap(
            z=pivot.values, x=list(range(24)), y=WEEKDAYS, xgap=2, ygap=2,
            zmin=0, zmax=100, colorscale=[[i / steps, colour] for i, colour in enumerate(ui.BLUE_RAMP)],
            colorbar=dict(title=dict(text="CPU %", font=dict(color=ui.INK_2)), thickness=12,
                          tickfont=dict(color=ui.MUTED)),
            hovertemplate="%{y} %{x}:00<br>Average SQL CPU %{z:.0f} %<extra></extra>"))
        fig.update_yaxes(autorange="reversed")
        fig.update_xaxes(tickmode="array", tickvals=list(range(0, 24, 3)),
                         ticktext=[f"{hr:02d}:00" for hr in range(0, 24, 3)], showgrid=False)
        st.plotly_chart(ui.style_figure(fig, 270, left_margin=50), theme=None, config={"displayModeBar": False})
        quiet = quietest_hour(pivot)
        busy = pivot.stack().dropna()
        if quiet and not busy.empty:
            busy_day, busy_hour = busy.idxmax()
            st.markdown(f"Quietest: **{quiet[0]} {quiet[1]:02d}:00** (average {quiet[2]:.0f} %) - a good "
                        "window for maintenance such as index rebuilds or `DBCC CHECKDB`. Busiest: "
                        f"**{WEEKDAYS[int(busy_day)]} {int(busy_hour):02d}:00** ({busy.max():.0f} %).")

# ---------------------------------------------------------------------------
# Sessions and blocking incidents
# ---------------------------------------------------------------------------
left, right = st.columns(2)
with left, st.container(border=True):
    st.markdown("**User sessions**" + (" (hourly average)" if bucket else ""))
    s = resample(sv, "collected_at", ["user_sessions"], bucket)
    fig = line_figure()
    fig.add_scatter(x=s["collected_at"], y=s["user_sessions"], mode=MODE, name="User sessions",
                    line=dict(color=ui.SERIES_1, width=2), marker=dict(size=8),
                    hovertemplate="%{y:.0f} sessions<extra></extra>")
    fig.update_layout(showlegend=False)
    fig.update_yaxes(rangemode="tozero")
    st.plotly_chart(ui.style_figure(fig, 230, left_margin=40), theme=None, config={"displayModeBar": False})
with right, st.container(border=True):
    st.markdown("**Blocked sessions**" + (" (most per hour)" if bucket else ""))
    b = resample(sv, "collected_at", ["blocked_sessions"], bucket, how="max")
    fig = time_axis(go.Figure())
    fig.add_bar(x=b["collected_at"], y=b["blocked_sessions"], name="Blocked sessions",
                marker=dict(color=ui.SERIES_2, cornerradius=2),
                hovertemplate="%{y:.0f} blocked<extra></extra>")
    fig.update_layout(hovermode="x unified")
    fig.update_layout(showlegend=False, bargap=0.1)
    fig.update_yaxes(rangemode="tozero", dtick=1)
    st.plotly_chart(ui.style_figure(fig, 230, left_margin=40), theme=None, config={"displayModeBar": False})

if incidents.empty:
    st.success("No blocking was seen in this period.", icon="✅")
else:
    st.markdown(f"**Blocking incidents** ({len(incidents)}) - consecutive snapshots with blocked sessions")
    st.dataframe(
        incidents.sort_values("start", ascending=False), hide_index=True,
        column_config={
            "start": st.column_config.DatetimeColumn("First seen", format="ddd DD MMM HH:mm"),
            "end": st.column_config.DatetimeColumn("Last seen", format="ddd DD MMM HH:mm"),
            "minutes": st.column_config.NumberColumn("About (min)", format="%d",
                                                     help="Snapshots are minutes apart, so this is approximate"),
            "max_blocked": st.column_config.NumberColumn("Most blocked", format="%d"),
            "longest_wait_s": st.column_config.NumberColumn("Longest wait (s)", format="%.0f"),
        })

# ---------------------------------------------------------------------------
# Long blocking: who blocked which query (sessions blocked >= BLOCKING_DETAIL_SECONDS)
# ---------------------------------------------------------------------------
limit = settings.blocking_detail_seconds
limit_text = f"{limit / 60:g} min" if limit >= 60 else f"{limit} s"
st.markdown(f"**Long blocking - who blocked which query** (sessions blocked for {limit_text} or longer)")
blk = h.blocking
if blk.empty:
    st.caption(f"None in this period. The collector checks every {settings.blocking_check_seconds} s; once a "
               f"session has waited {limit_text}, it saves the blocked query and the session blocking it.")
else:
    def txt(value, default: str = "?") -> str:
        """Text or a default (missing values may arrive as None or NaN)."""
        return value if isinstance(value, str) and value.strip() else default

    def who(session, login, program) -> str:
        return f"{int(session)} · {txt(login)} ({txt(program, 'no program name')})"

    view = blk.sort_values("blocked_since", ascending=False).reset_index(drop=True)
    view = view.assign(
        waited_min=view["longest_wait_s"] / 60,
        blocked=[who(*x) for x in zip(view["blocked_session"], view["blocked_login"], view["blocked_program"])],
        blocker=[who(*x) for x in zip(view["head_blocker"], view["head_login"], view["head_program"])])
    event = st.dataframe(
        view, hide_index=True, on_select="rerun", selection_mode="single-row", key="hist_long_blocks",
        column_order=["blocked_since", "waited_min", "blocked", "database_name", "wait_type", "blocker",
                      "head_status"],
        column_config={
            "blocked_since": st.column_config.DatetimeColumn("Blocked from", format="ddd DD MMM HH:mm"),
            "waited_min": st.column_config.NumberColumn("Waited (min)", format="%.1f",
                                                        help="Longest wait the collector saw"),
            "blocked": st.column_config.TextColumn("Blocked session"),
            "database_name": st.column_config.TextColumn("Database"),
            "wait_type": st.column_config.TextColumn("Wait type"),
            "blocker": st.column_config.TextColumn("Blocked by (head blocker)"),
            "head_status": st.column_config.TextColumn("Head blocker was"),
        })
    picked_rows = event.selection.rows if event and event.selection else []
    r = view.iloc[picked_rows[0] if picked_rows else 0]
    with st.container(border=True):
        st.markdown(
            f"**Session {int(r['blocked_session'])}** waited **{r['waited_min']:.1f} min** "
            f"({txt(r['wait_type'], 'lock wait')} on `{txt(r['wait_resource'], 'n/a')}`) in "
            f"**{txt(r['database_name'])}** · chain {txt(r['chain'], '-')}"
            + ("" if picked_rows else " · *the most recent - select a row above for another*"))
        left, right = st.columns(2)
        with left:
            st.markdown(f"**Blocked query** - session {int(r['blocked_session'])} · {txt(r['blocked_login'])} · "
                        f"{txt(r['blocked_program'], 'no program name')} · host {txt(r['blocked_host'])}")
            st.code(txt(r["blocked_sql"], "-- statement not available"), language="sql", wrap_lines=True)
        with right:
            direct = ("" if r["blocked_by"] == r["head_blocker"]
                      else f" · it waited directly for session {int(r['blocked_by'])}")
            st.markdown(f"**Blocking session** - head blocker {int(r['head_blocker'])} · {txt(r['head_login'])} · "
                        f"{txt(r['head_program'], 'no program name')} · host {txt(r['head_host'])} · "
                        f"{txt(r['head_status'], '')}{direct}")
            st.code(txt(r["head_sql"], "-- statement not available"), language="sql", wrap_lines=True)
        open_tran = pd.to_numeric(r["head_open_tran"], errors="coerce")
        if txt(r["head_status"], "").startswith("sleeping") and pd.notna(open_tran) and open_tran > 0:
            st.caption("The head blocker was idle with an open transaction: it ran the statement above and never "
                       "committed or rolled back. Contact the owner of that login / program.")
        ui.footnote("The blocking session's statement is its current one, or its last one if it was idle. "
                    "This dashboard never kills sessions.")

# ---------------------------------------------------------------------------
# Databases: growth (capacity planning) and transaction-log use
# ---------------------------------------------------------------------------
st.subheader("Databases")
dbs = h.databases
if dbs.empty:
    st.info("No database snapshots in this period (the server was unreachable).", icon="🔎")
else:
    all_names = sorted(full.databases["database_name"].unique(), key=str.lower)
    colour = {n: ui.CATEGORICAL[i % len(ui.CATEGORICAL)] for i, n in enumerate(all_names)}  # fixed per DB
    latest = dbs.sort_values("collected_at").groupby("database_name").last()
    by_size = (latest["data_mb"] + latest["log_mb"].fillna(0)).sort_values(ascending=False)
    user_dbs = [n for n in by_size.index if not latest.loc[n, "is_system"]]
    picked = st.multiselect("Databases to compare", list(by_size.index), default=(user_dbs or list(by_size.index))[:8],
                            max_selections=8, key="hist_dbs",
                            help="Up to 8 at a time, so every line keeps a clearly different colour.")
    chosen = dbs[dbs["database_name"].isin(picked)].assign(
        total_mb=lambda d: d["data_mb"] + d["log_mb"].fillna(0))

    left, right = st.columns(2)
    with left, st.container(border=True):
        st.markdown("**Size - data + log (MB)**")
        fig = line_figure()
        for name in picked:
            d = resample(chosen[chosen["database_name"] == name], "collected_at", ["total_mb"], bucket)
            fig.add_scatter(x=d["collected_at"], y=d["total_mb"], mode=MODE, name=name,
                            line=dict(color=colour[name], width=2), marker=dict(size=8),
                            hovertemplate=name + " %{y:,.0f} MB<extra></extra>")
        fig.update_yaxes(rangemode="tozero", tickformat=",.0f")
        st.plotly_chart(ui.style_figure(fig, 280, left_margin=60), theme=None, config={"displayModeBar": False})
    with right, st.container(border=True):
        st.markdown("**Transaction log used (%)**" + (" (hourly average)" if bucket else ""))
        fig = line_figure()
        for name in picked:
            d = resample(chosen[chosen["database_name"] == name], "collected_at", ["log_used_pct"], bucket)
            fig.add_scatter(x=d["collected_at"], y=d["log_used_pct"], mode=MODE, name=name,
                            line=dict(color=colour[name], width=2), marker=dict(size=8),
                            hovertemplate=name + " %{y:.0f} %<extra></extra>")
        for level, label in ((settings.log_used_warning_pct, "Warning"),
                             (settings.log_used_critical_pct, "Critical")):
            fig.add_hline(y=level, line=dict(color=ui.MUTED, width=1, dash="dash"),
                          annotation_text=f"{label} {level}%", annotation_position="top left",
                          annotation_font=dict(color=ui.INK_2, size=11))
        fig.update_yaxes(range=[0, 100], ticksuffix=" %")
        st.plotly_chart(ui.style_figure(fig, 280, left_margin=50), theme=None, config={"displayModeBar": False})

    st.markdown("**Growth and 30-day projection** - trend line through every snapshot in the range")
    table = growth[growth["database_name"].isin(picked)]
    st.dataframe(
        table, hide_index=True,
        column_order=["database_name", "start_mb", "end_mb", "growth_mb", "mb_per_day", "projected_mb"],
        column_config={
            "database_name": st.column_config.TextColumn("Database"),
            "start_mb": st.column_config.NumberColumn("Size at start (MB)", format="%.0f"),
            "end_mb": st.column_config.NumberColumn("Size now (MB)", format="%.0f"),
            "growth_mb": st.column_config.NumberColumn("Growth (MB)", format="%+.0f"),
            "mb_per_day": st.column_config.NumberColumn("MB per day", format="%+.1f"),
            "projected_mb": st.column_config.NumberColumn("In 30 days (MB)", format="%.0f",
                                                          help="Size now + 30 × MB per day"),
        })

# ---------------------------------------------------------------------------
# Export + sources
# ---------------------------------------------------------------------------
st.download_button("Download these snapshots (CSV)", sv.to_csv(index=False).encode("utf-8"),
                   file_name=f"{target.name}_history_{choice.replace(' ', '')}.csv", mime="text/csv",
                   icon=":material/download:")
source = ("generated SAMPLE history (demo mode)" if full.sample else
          f"history.db, written every {settings.history_interval_seconds // 60} min by the history "
          f"collector (tools/collect_history.py) from the same DMVs as the live pages; kept "
          f"{settings.history_retention_days} days")
ui.footnote(f"Source: {source}. This page never queries SQL Server itself.")
