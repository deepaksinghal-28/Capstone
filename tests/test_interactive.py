"""
Interactive features: health score, CPU trend, drill-down, blocking tree,
acknowledge / snooze.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from monitor import demo_data
from monitor.alerts import AlertCandidate, AlertStore, health_score
from monitor.collectors import collect_cpu_history
from monitor.config import get_settings

APP_FILE = Path(__file__).resolve().parent.parent / "app.py"
NOW = datetime(2026, 11, 9, 22, 0, 0)


# --- Health score -----------------------------------------------------------------------

def _active(*severities):
    return pd.DataFrame([{"name": f"Alert {i}", "target": "db", "severity": s,
                          "severity_rank": {"Critical": 0, "Warning": 1, "Info": 2}[s]}
                         for i, s in enumerate(severities)])


def test_health_score_rules():
    assert health_score(True, pd.DataFrame()) == (100, "Good", [])
    score, rating, reasons = health_score(True, _active("Critical", "Warning", "Warning"))
    assert (score, rating) == (64, "Fair") and len(reasons) == 3       # 100 - 20 - 8 - 8
    assert health_score(True, _active(*["Critical"] * 6))[:2] == (0, "Poor")   # never below 0
    assert health_score(False, pd.DataFrame())[:2] == (0, "Poor")


# --- Acknowledge / snooze ----------------------------------------------------------------

def _cand():
    return AlertCandidate("log_space", "Transaction log nearly full", "Critical", "FinanceDB",
                          "93% used", ["step"], server="DEMO-PROD-02")


def test_acknowledge_and_snooze_are_local_and_reversible(tmp_path):
    store = AlertStore(tmp_path / "a.db")
    store.sync([_cand()], NOW)
    key = _cand().key
    store.acknowledge(key, "tester", NOW)
    store.snooze(key, 60, NOW)
    row = store.frame("ACTIVE", now=NOW + timedelta(minutes=10)).iloc[0]
    assert row["is_acknowledged"] and row["acknowledged_by"] == "tester" and row["is_snoozed"]
    assert not store.frame("ACTIVE", now=NOW + timedelta(minutes=61)).iloc[0]["is_snoozed"]  # expires
    store.unacknowledge(key)
    store.unsnooze(key)
    row = store.frame("ACTIVE", now=NOW).iloc[0]
    assert not row["is_acknowledged"] and not row["is_snoozed"]


def test_reopened_alert_starts_unacknowledged(tmp_path):
    store = AlertStore(tmp_path / "a.db")
    store.sync([_cand()], NOW)
    store.acknowledge(_cand().key, "tester", NOW)
    store.sync([], NOW + timedelta(minutes=1))                          # resolved
    store.sync([_cand()], NOW + timedelta(minutes=5))                   # comes back = new incident
    assert not store.frame("ACTIVE").iloc[0]["is_acknowledged"]


# --- CPU trend ---------------------------------------------------------------------------------

def test_demo_cpu_history_has_an_hour_of_points():
    cpu = demo_data.cpu_history("demo-prod-01.example.local")
    assert cpu.connected and len(cpu.points) == 60
    assert cpu.points["sql_cpu_pct"].between(0, 100).all()


@pytest.mark.integration
def test_real_cpu_history_from_ring_buffer():
    cpu = collect_cpu_history(get_settings(), minutes=60)
    assert cpu.connected, cpu.error
    assert 0 < len(cpu.points) <= 60                                   # one point per minute
    assert cpu.points["event_time"].is_monotonic_increasing


# --- UI (demo mode: needs no SQL Server and has blocking + alerts to click) -------------------

@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    st.cache_data.clear()


def _run(page=None, server="DEMO-PROD-01"):
    at = AppTest.from_file(str(APP_FILE), default_timeout=60)
    at.session_state["selected_server"] = server
    at.run()
    assert not at.exception, at.exception
    if page:
        at.switch_page(page).run()
        assert not at.exception, at.exception
    return at


def test_summary_has_score_cards_and_cpu_trend(demo):
    at = _run()
    text = " ".join(m.value for m in at.markdown)
    assert "Health score" in text and "CPU - last 60 minutes" in text
    labels = [m.label for m in at.metric]
    for expected in ("Blocked sessions", "SQL Server CPU", "Highest log use", "Backup issues", "Alerts"):
        assert expected in labels


def test_blocking_tree_click_shows_session_details(demo):
    at = _run("views/sessions_blocking.py")
    keys = {b.key for b in at.button}
    assert {"node_0_72", "node_0_75", "node_0_78"} <= keys               # 72 → 75 → 78
    at.button(key="node_0_72").click().run()
    assert not at.exception, at.exception
    assert any("Session 72" in m.value and "head blocker" in m.value for m in at.markdown)


def test_health_drill_down_by_database(demo):
    at = _run("views/health.py", server="DEMO-PROD-02")
    at.selectbox(key="health_db_pick").select("FinanceDB").run()
    assert not at.exception, at.exception
    labels = [m.label for m in at.metric]
    assert {"Data files", "Log used", "Last full backup"} <= set(labels)
    assert any("LOG_BACKUP" in m.value for m in at.markdown)            # why the log can't be reused


def test_alert_acknowledge_and_snooze_buttons(demo):
    at = _run("views/alerts.py", server="DEMO-PROD-02")
    ack = [b for b in at.button if str(b.key).startswith("ack_")]
    assert ack
    ack[0].click().run()
    assert not at.exception, at.exception
    assert any("Acknowledged by demo visitor" in m.value for m in at.markdown)
    snooze = [b for b in at.button if str(b.key).startswith("snooze_")]
    snooze[0].click().run()
    assert any("snoozed alert(s) hidden" in c.value for c in at.caption)
