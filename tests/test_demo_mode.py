"""
DEMO_MODE tests - the public demo must show every page with SAMPLE data and
must never contact SQL Server.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from monitor import db, demo_data
from monitor.alerts import evaluate_rules
from monitor.config import DEMO_TARGETS, get_settings, server_targets, settings_for

APP_FILE = Path(__file__).resolve().parent.parent / "app.py"


@pytest.fixture(autouse=True)
def demo_on(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")

    def no_sql(*args, **kwargs):
        raise AssertionError("DEMO_MODE must never connect to SQL Server")

    if db.pyodbc is not None:
        monkeypatch.setattr(db.pyodbc, "connect", no_sql)
    st.cache_data.clear()


def test_demo_uses_fictional_servers_only():
    s = get_settings()
    assert s.demo_mode is True
    assert server_targets(s) == DEMO_TARGETS
    assert all(".example.local" in t.server for t in DEMO_TARGETS)   # reserved, non-real domain


def test_demo_connect_is_refused_even_if_called():
    with pytest.raises(db.DatabaseError):
        with db.connect(get_settings()):
            pass


def test_demo_snapshots_have_the_real_shapes():
    prod1 = "demo-prod-01.example.local"
    h = demo_data.health(prod1)
    assert h.connected and h.server["product_version"] == "16.0.4305.5"
    assert {"health_status", "log_used_pct"} <= set(h.databases.columns)
    s = demo_data.sessions(prod1)
    assert list(s.chains.sort_values("depth")["chain"])[-1] == "72 → 75 → 78"
    q = demo_data.query_stats(prod1, "cpu")
    assert {"avg_cpu_ms", "short_text"} <= set(q.queries.columns) and len(q.queries) >= 5
    assert not demo_data.health("demo-dev-01.example.local").connected


def test_demo_triggers_every_kind_of_alert():
    base = get_settings()
    keys = set()
    for t in DEMO_TARGETS:
        st_ = settings_for(base, t.server)
        h = demo_data.health(t.server)
        alerts = evaluate_rules(h, demo_data.sessions(t.server), demo_data.backups(t.server),
                                demo_data.memory(t.server) if h.connected else None, st_,
                                server_name=t.name)
        keys |= {(a.server, a.rule, a.severity) for a in alerts}
    expected = {
        ("DEMO-PROD-01", "blocking", "Critical"),
        ("DEMO-PROD-02", "log_space", "Critical"),
        ("DEMO-PROD-02", "backup_overdue", "Warning"),
        ("DEMO-PROD-02", "memory_pressure", "Warning"),
        ("DEMO-UAT-01", "backup_missing", "Critical"),
        ("DEMO-REPORT-01", "database_not_online", "Critical"),
        ("DEMO-REPORT-01", "long_running", "Warning"),
        ("DEMO-DEV-01", "connection_failure", "Critical"),
    }
    assert expected <= keys


def _run(page: str | None = None, server: str | None = None) -> AppTest:
    at = AppTest.from_file(str(APP_FILE), default_timeout=60)
    if server:
        at.session_state["selected_server"] = server
    at.run()
    assert not at.exception, at.exception
    if page:
        at.switch_page(page).run()
        assert not at.exception, at.exception
    return at


def test_demo_switches_on_automatically_without_sql_driver(monkeypatch):
    from monitor import config
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setattr(config, "odbc_driver_available", lambda driver: False)  # e.g. Streamlit Cloud
    assert config.get_settings().demo_mode is True
    monkeypatch.setenv("DEMO_MODE", "false")                                     # explicit setting wins
    assert config.get_settings().demo_mode is False


def test_real_mode_stays_off_where_the_driver_exists(monkeypatch):
    from monitor import config
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setattr(config, "odbc_driver_available", lambda driver: True)   # this PC
    assert config.get_settings().demo_mode is False


def test_demo_opens_with_a_server_and_every_module_has_data():
    at = _run()                                                      # nothing selected by the visitor
    assert at.session_state["selected_server"] == "DEMO-PROD-01"
    assert "Uptime" in [m.label for m in at.metric]                 # summary shown straight away
    assert {f"demo_{t.name}" for t in DEMO_TARGETS} <= {b.key for b in at.button}
    for page in ("views/health.py", "views/query_performance.py", "views/sessions_blocking.py",
                 "views/backups.py", "views/alerts.py"):
        at.switch_page(page).run()
        assert not at.exception, (page, at.exception)
        assert not any("No server selected" in i.value for i in at.info), page


def test_demo_banner_and_search():
    at = _run()
    assert any("SAMPLE data" in w.value for w in at.warning)          # labelled on every page
    at.text_input(key="srv_search").set_value("prod").run()
    assert {b.key for b in at.button} >= {"pick_DEMO-PROD-01", "pick_DEMO-PROD-02"}
    at.button(key="pick_DEMO-PROD-01").click().run()
    assert not at.exception, at.exception
    assert at.session_state["selected_server"] == "DEMO-PROD-01"


@pytest.mark.parametrize("page", ["views/health.py", "views/query_performance.py",
                                  "views/sessions_blocking.py", "views/backups.py", "views/alerts.py"])
def test_every_demo_page_renders(page):
    at = _run(page, server="DEMO-PROD-01")
    assert any("SAMPLE data" in w.value for w in at.warning)
    if page.endswith("sessions_blocking.py"):
        assert any("Blocking detected" in e.value for e in at.error)


def test_unreachable_demo_server_page():
    at = _run("views/health.py", server="DEMO-DEV-01")
    assert any("is unavailable" in e.value for e in at.error)
