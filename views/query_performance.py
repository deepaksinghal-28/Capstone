"""
Module 2 - SQL Query Performance Analyzer.

Shows the most resource-consuming query shapes in the plan cache:
execution count, total/average CPU, total/average duration, logical reads,
query text. Source: sys.dm_exec_query_stats + sys.dm_exec_sql_text
                    + sys.dm_exec_plan_attributes
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from monitor import ui
from monitor.collectors import QUERY_METRICS, rank_queries

ui.page_header("Query Performance")
target = ui.require_server()

# ---------------------------------------------------------------------------
# Controls (one row, above everything they affect)
# ---------------------------------------------------------------------------
metric_keys = list(QUERY_METRICS)
c1, c2, c3, c4 = st.columns([2, 1, 2, 2])
metric = c1.selectbox("Rank by", metric_keys, format_func=lambda k: QUERY_METRICS[k][0],
                      key="qp_metric")
top_n = c2.selectbox("Show top", [10, 20, 50], index=0, key="qp_top")

snap = ui.query_stats_of(target.server, metric)
if not snap.connected:
    ui.show_unavailable(snap.error, snap.error_detail, snap.collected_at_local)
    st.stop()

all_queries = snap.queries
db_options = ["All databases"] + (sorted(all_queries["database_name"].unique().tolist())
                                  if not all_queries.empty else [])
database = c3.selectbox("Database", db_options, key="qp_db")
search = c4.text_input("Search query text", placeholder="e.g. Orders, usp_…")
hide_monitor = st.toggle("Hide this dashboard's own monitoring queries", value=True,
                         help="Every query sent by this app starts with /* DBMonitor */ "
                              "so it can be recognised and hidden here.")

# ---------------------------------------------------------------------------
# What the numbers mean (plan cache is NOT a complete history)
# ---------------------------------------------------------------------------
cache = snap.cache
oldest = cache.get("oldest_plan_cached")
since_text = (f"since **{pd.Timestamp(oldest):%Y-%m-%d %H:%M}** (server clock)"
              if oldest is not None and not pd.isna(oldest) else "— the plan cache is empty")
st.info(
    f"Statistics come from the **plan cache** {since_text}: "
    f"{int(cache.get('cached_statements') or 0):,} cached statements, "
    f"{int(cache.get('distinct_query_shapes') or 0):,} distinct query shapes. "
    "Totals start when a plan is cached and are lost when it is evicted (memory pressure, "
    "recompile, `DBCC FREEPROCCACHE`, service restart). Queries using `OPTION (RECOMPILE)` "
    "do not appear. Treat this as *recent* activity, not a full history.",
    icon="ℹ️",
)

ranked = rank_queries(all_queries, metric, top_n, hide_monitor=hide_monitor,
                      database=None if database == "All databases" else database,
                      search=search)

if ranked.empty:
    st.info(
        "No queries match the current filters. On a quiet server the plan cache holds "
        "only system activity: run the demo workload "
        "(`tools\\generate_query_workload.py`) to create measurable query activity.",
        icon="🔎",
    )
    st.stop()

# ---------------------------------------------------------------------------
# Summary cards for the rows shown
# ---------------------------------------------------------------------------
def fmt_ms(ms: float) -> str:
    """1234 -> '1.2 s', 5.9 -> '5.9 ms'."""
    return f"{ms / 1000:,.1f} s" if ms >= 1000 else f"{ms:,.1f} ms"


W = ui.CARD_WIDTH
total_reads = ranked["total_logical_reads"].sum()
with st.container(horizontal=True, wrap=True, gap="small"):
    st.metric("Queries shown", f"{len(ranked)}", border=True, width=W)
    st.metric("Executions", f"{int(ranked['execution_count'].sum()):,}", border=True, width=W)
    st.metric("Total CPU", fmt_ms(ranked["total_cpu_ms"].sum()), border=True, width=W,
              help="Sum of worker (CPU) time for the queries shown")
    st.metric("Total duration", fmt_ms(ranked["total_duration_ms"].sum()),
              border=True, width=W, help="Sum of elapsed time for the queries shown")
    st.metric("Logical reads", f"{int(total_reads):,}", border=True, width=W,
              help=f"8 KB pages read from the buffer pool = {total_reads * 8 / 1024:,.1f} MB")

# ---------------------------------------------------------------------------
# Ranking chart (single series -> one colour, title names the metric)
# ---------------------------------------------------------------------------
label, column = QUERY_METRICS[metric]
chart_df = ranked.head(10).iloc[::-1]          # biggest at the top
y_labels = [f"#{r.rank} · {r.object_name or r.short_text[:28]}" for r in chart_df.itertuples()]
with st.container(border=True):
    st.markdown(f"**Top {len(chart_df)} by {label[0].lower() + label[1:]}**")
    fig = go.Figure()
    fig.add_bar(
        y=y_labels, x=chart_df[column], orientation="h",
        marker=dict(color=ui.SERIES_1, cornerradius=4), showlegend=False,
        customdata=chart_df[["database_name", "execution_count", "avg_cpu_ms",
                             "avg_duration_ms", "avg_logical_reads"]].to_numpy(),
        hovertemplate=("<b>%{y}</b><br>" + label + ": %{x:,.1f}<br>"
                       "Database: %{customdata[0]}<br>Executions: %{customdata[1]:,}<br>"
                       "Avg CPU: %{customdata[2]:,.2f} ms · Avg duration: %{customdata[3]:,.2f} ms"
                       "<br>Avg logical reads: %{customdata[4]:,.0f}<extra></extra>"),
    )
    st.plotly_chart(ui.style_figure(fig, 60 + 34 * len(chart_df), left_margin=220),
                    theme=None, config={"displayModeBar": False})

# ---------------------------------------------------------------------------
# Detail table - select a row to see the full statement
# ---------------------------------------------------------------------------
st.subheader("Query details")
st.caption("Times in milliseconds; reads in 8 KB pages. Select a row to see its full SQL text.")
event = st.dataframe(
    ranked,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_order=["rank", "database_name", "object_name", "execution_count",
                  "total_cpu_ms", "avg_cpu_ms", "total_duration_ms", "avg_duration_ms",
                  "max_duration_ms", "avg_logical_reads", "total_logical_reads",
                  "seconds_since_last_run", "short_text"],
    column_config={
        "rank": st.column_config.NumberColumn("#", format="%d", width="small"),
        "database_name": st.column_config.TextColumn("Database"),
        "object_name": st.column_config.TextColumn("Procedure / object"),
        "execution_count": st.column_config.NumberColumn("Executions", format="localized"),
        "total_cpu_ms": st.column_config.NumberColumn("Total CPU (ms)", format="%.1f"),
        "avg_cpu_ms": st.column_config.NumberColumn("Avg CPU (ms)", format="%.2f"),
        "total_duration_ms": st.column_config.NumberColumn("Total duration (ms)", format="%.1f"),
        "avg_duration_ms": st.column_config.NumberColumn("Avg duration (ms)", format="%.2f"),
        "max_duration_ms": st.column_config.NumberColumn("Max duration (ms)", format="%.1f"),
        "avg_logical_reads": st.column_config.NumberColumn("Avg reads", format="%.0f"),
        "total_logical_reads": st.column_config.NumberColumn("Total reads", format="localized"),
        "seconds_since_last_run": st.column_config.NumberColumn("Last run (s ago)", format="localized"),
        "short_text": st.column_config.TextColumn("Query text (start)", width="large"),
    },
)

selected_rows = event.selection.rows if event and event.selection else []
if selected_rows:
    row = ranked.iloc[selected_rows[0]]
    with st.container(border=True):
        st.markdown(f"**#{row['rank']} · {row['database_name']}"
                    f"{' · ' + row['object_name'] if row['object_name'] else ''}**  ·  "
                    f"query_hash `{row['query_hash']}` · {int(row['cached_plan_count'])} cached plan(s)")
        st.code(row["query_text"], language="sql")
else:
    ui.footnote("Tip: click a row in the table to show its complete SQL statement here.")

ui.footnote(
    f"Last collected {snap.collected_at_local:%Y-%m-%d %H:%M:%S} (this PC) · "
    "Sources: sys.dm_exec_query_stats, sys.dm_exec_sql_text, sys.dm_exec_plan_attributes · "
    "CPU and duration are reported by SQL Server in microseconds and shown here in milliseconds."
)
