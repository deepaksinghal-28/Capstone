"""
Module 2 - SQL Query Performance Analyzer.

Shows the most resource-consuming query shapes in the plan cache:
execution count, total/average CPU, total/average duration, logical reads,
query text. Source: sys.dm_exec_query_stats + sys.dm_exec_sql_text
                    + sys.dm_exec_plan_attributes
"""

import altair as alt
import pandas as pd
import streamlit as st

from monitor import ui
from monitor.collectors import ALL_DATABASES, QUERY_METRICS, database_filter_options, rank_queries

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
# Every database on the server is offered, not only those that happen to be in the plan cache
health = ui.health_of(target.server)
db_options = database_filter_options(all_queries, health.databases if health.connected else None)
database = c3.selectbox("Database", db_options, key="qp_db",
                        help="Every database on this server. One with no recently run queries shows "
                             "nothing until queries run against it.")
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

one_db = None if database == ALL_DATABASES else database
ranked = rank_queries(all_queries, metric, top_n, hide_monitor=hide_monitor, database=one_db,
                      search=search)

if ranked.empty:
    db_rows = (all_queries[all_queries["database_name"] == one_db]
               if one_db and not all_queries.empty else all_queries)
    if one_db and db_rows.empty:
        since = (f"since {pd.Timestamp(oldest):%Y-%m-%d %H:%M} (server clock)"
                 if oldest is not None and not pd.isna(oldest) else "at the moment")
        message = (f"**{one_db}** has no queries in the plan cache {since}. The plan cache only "
                   f"holds queries that ran recently, so this list fills once queries run against "
                   f"{one_db}; then click **Refresh now**.")
    elif one_db and hide_monitor and "is_monitor_query" in db_rows and (db_rows["is_monitor_query"] == 1).all():
        message = (f"The only cached queries for **{one_db}** are this dashboard's own monitoring "
                   "queries. Switch off *Hide this dashboard's own monitoring queries* to see them.")
    else:
        message = ("No queries match the current filters. On a quiet server the plan cache holds "
                   "only system activity: run the demo workload "
                   "(`tools\\generate_query_workload.py`) to create measurable query activity.")
    st.info(message, icon="🔎")
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
chart_cols = list(dict.fromkeys(["rank", column, "database_name", "execution_count", "avg_cpu_ms",
                                 "avg_duration_ms", "avg_logical_reads"]))   # no duplicates
chart_df = ranked.head(10)[chart_cols].copy()
chart_df["label"] = [f"#{r.rank} · {r.object_name or r.short_text[:28]}"
                     for r in ranked.head(10).itertuples()]
with st.container(border=True):
    st.markdown(f"**Top {len(chart_df)} by {label[0].lower() + label[1:]}**")
    # Clickable Altair chart: click a bar to select that query (double-click clears).
    qp_pick = alt.selection_point(name="pick", fields=["rank"], on="click", clear="dblclick")
    qp_chart = alt.Chart(chart_df).mark_bar(cornerRadiusEnd=3, color=ui.SERIES_1, cursor="pointer").encode(
        y=alt.Y("label:N", sort=alt.EncodingSortField(field="rank", order="ascending"), title=None),
        x=alt.X(f"{column}:Q", title=label),
        opacity=alt.condition(qp_pick, alt.value(1.0), alt.value(0.35)),
        tooltip=[alt.Tooltip("label:N", title="Query"), alt.Tooltip(f"{column}:Q", title=label, format=",.1f"),
                 alt.Tooltip("database_name:N", title="Database"),
                 alt.Tooltip("execution_count:Q", title="Executions", format=","),
                 alt.Tooltip("avg_cpu_ms:Q", title="Avg CPU (ms)", format=",.2f"),
                 alt.Tooltip("avg_duration_ms:Q", title="Avg duration (ms)", format=",.2f"),
                 alt.Tooltip("avg_logical_reads:Q", title="Avg logical reads", format=",.0f")],
    ).add_params(qp_pick)
    chart_event = st.altair_chart(ui.style_altair(qp_chart, 40 + 30 * len(chart_df)), theme=None,
                                  key="qp_chart", on_select="rerun", selection_mode="pick")
    st.caption("Tip: click a bar to see that query's full SQL text below (double-click to clear).")


def clicked_rank(event) -> int | None:
    """Rank number of the clicked bar, or None."""
    value = ui.altair_pick(event, "rank")
    return int(value) if value is not None else None

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
chart_rank = clicked_rank(chart_event)
row = None
if selected_rows:                                   # table selection wins
    row = ranked.iloc[selected_rows[0]]
elif chart_rank is not None and chart_rank in set(ranked["rank"]):
    row = ranked[ranked["rank"] == chart_rank].iloc[0]
if row is not None:
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
