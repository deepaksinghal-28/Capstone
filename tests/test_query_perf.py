"""
Module 2 tests - query performance logic and the plan-cache query.

TC-04  Query performance data (unit conversions, ranking, filtering, real DMV query)
"""

from __future__ import annotations

import pandas as pd
import pytest

from monitor.collectors import (ALL_DATABASES, QUERY_METRICS, collect_query_stats, database_filter_options,
                                prepare_query_stats, rank_queries)
from monitor.db import MONITOR_MARKER, run_query
from tools.run_sql_script import split_batches


def _raw() -> pd.DataFrame:
    return pd.DataFrame([
        # hash, execs, cpu_us,     elapsed_us, reads,  max_el,  db,            object,          monitor, text
        ("0x1", 1000, 2_000_000,   3_000_000,  5_000,  9_000,   "CapstoneDemo", "usp_Cheap",     0, "SELECT 1"),
        ("0x2",   10, 5_000_000,   6_000_000,  900_000, 800_000, "CapstoneDemo", "usp_Heavy",     0, "SELECT  big\n  scan"),
        ("0x3",   50,   100_000,     200_000,  100,    5_000,   None,          None,            0, None),
        ("0x4",  500, 9_000_000,   9_500_000,  1_000,  1_000,   "master",       None,            1, "/* DBMonitor */ SELECT ..."),
    ], columns=["query_hash", "execution_count", "total_cpu_us", "total_elapsed_us",
                "total_logical_reads", "max_elapsed_us", "database_name", "object_name",
                "is_monitor_query", "query_text"])


def test_prepare_converts_microseconds_to_milliseconds():
    df = prepare_query_stats(_raw()).set_index("query_hash")
    assert df.loc["0x1", "total_cpu_ms"] == pytest.approx(2000.0)
    assert df.loc["0x1", "avg_cpu_ms"] == pytest.approx(2.0)          # 2,000 ms / 1,000 execs
    assert df.loc["0x2", "avg_duration_ms"] == pytest.approx(600.0)   # 6,000 ms / 10 execs
    assert df.loc["0x2", "avg_logical_reads"] == pytest.approx(90_000)
    assert df.loc["0x2", "max_duration_ms"] == pytest.approx(800.0)
    assert df.loc["0x3", "database_name"] == "(system)"
    assert df.loc["0x2", "short_text"] == "SELECT big scan"            # whitespace collapsed


def test_prepare_handles_zero_executions():
    raw = _raw()
    raw.loc[0, "execution_count"] = 0
    df = prepare_query_stats(raw)
    assert pd.isna(df.loc[0, "avg_cpu_ms"])                           # no divide-by-zero crash


@pytest.mark.parametrize("metric, expected_first", [
    ("cpu", "0x2"), ("executions", "0x1"), ("reads", "0x2"), ("avg_duration", "0x2"),
])
def test_rank_queries_orders_by_metric_and_hides_monitor(metric, expected_first):
    ranked = rank_queries(prepare_query_stats(_raw()), metric, top_n=10)
    assert ranked.iloc[0]["query_hash"] == expected_first
    assert "0x4" not in set(ranked["query_hash"])                    # monitor query hidden
    assert list(ranked["rank"]) == list(range(1, len(ranked) + 1))


def test_rank_queries_filters():
    df = prepare_query_stats(_raw())
    assert set(rank_queries(df, "cpu", 10, hide_monitor=False)["query_hash"]) == {"0x1", "0x2", "0x3", "0x4"}
    assert list(rank_queries(df, "cpu", 10, database="CapstoneDemo")["query_hash"]) == ["0x2", "0x1"]
    assert list(rank_queries(df, "cpu", 10, search="heavy")["query_hash"]) == ["0x2"]
    assert len(rank_queries(df, "cpu", top_n=1)) == 1


def test_database_filter_lists_server_databases_even_without_cached_queries():
    queries = prepare_query_stats(_raw())          # CapstoneDemo, master and "(system)" (no database)
    server_dbs = pd.DataFrame({"database_name": ["master", "tempdb", "model", "msdb", "CapstoneDemo",
                                                 "SalesArchive", None]})
    options = database_filter_options(queries, server_dbs)
    assert options[0] == ALL_DATABASES
    assert options[1:] == ["(system)", "CapstoneDemo", "master", "model", "msdb", "SalesArchive", "tempdb"]
    # without the server list (server unreachable) the cached databases are still offered
    assert database_filter_options(queries, None) == [ALL_DATABASES, "(system)", "CapstoneDemo", "master"]
    assert database_filter_options(pd.DataFrame(), pd.DataFrame()) == [ALL_DATABASES]


def test_rank_queries_rejects_unknown_metric():
    with pytest.raises(ValueError):
        rank_queries(prepare_query_stats(_raw()), "drop table", 10)


def test_split_batches_like_ssms():
    script = "SELECT 1;\nGO\n\n  go  \nSELECT 2;\nGO -- end\nSELECT 'GO inside text';"
    assert split_batches(script) == ["SELECT 1;", "SELECT 2;", "SELECT 'GO inside text';"]


# --- Integration: real plan cache --------------------------------------------------

# NOTE: on this Cloud PC Windows sends SQL Server frequent low-memory notifications
# (RING_BUFFER_RESOURCE_MONITOR: RESOURCE_MEMPHYSICAL_LOW) and SQL Server trims the
# plan cache each time. Each test therefore runs a query immediately before reading
# the cache instead of assuming earlier plans are still there.

@pytest.mark.integration
@pytest.mark.parametrize("metric", list(QUERY_METRICS))
def test_tc04_top_queries_from_real_plan_cache(metric):
    # Windows low-memory notifications can empty the plan cache at any moment on
    # this PC, so run a query and read the cache again if it was just flushed.
    for _attempt in range(3):
        run_query("SELECT COUNT(*) AS n FROM sys.objects;")   # guarantees >= 1 cached plan
        snap = collect_query_stats(metric, fetch_rows=20)
        assert snap.connected, snap.error
        if not snap.queries.empty:
            break
    assert not snap.queries.empty
    for col in ("total_cpu_ms", "avg_cpu_ms", "avg_duration_ms", "avg_logical_reads", "query_text"):
        assert col in snap.queries.columns
    assert snap.cache["cached_statements"] >= len(snap.queries)
    assert (snap.queries["total_cpu_ms"] >= 0).all()


@pytest.mark.integration
def test_tc04_monitor_queries_are_tagged():
    run_query("SELECT COUNT(*) AS n FROM sys.databases;")          # sent with the marker
    snap = collect_query_stats("executions", fetch_rows=200)
    tagged = snap.queries[snap.queries["is_monitor_query"] == 1]
    assert not tagged.empty
    assert MONITOR_MARKER  # marker constant exists and is used by run_query
