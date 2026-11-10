"""
Dashboard tests - run the Streamlit app headlessly (no browser) against the
REAL local SQL Server instances, then check what the pages show.

Test cases covered:
  TC-01  Dashboard shows "Connected" and summary cards
  TC-02  An unreachable server shows a clear error (and does not break other servers)
  TC-10  Search / filter / refresh controls work without errors
  Search-first GUI: nothing listed until you search; one server shown at a time
"""

from __future__ import annotations

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

pytestmark = pytest.mark.integration

APP_FILE = Path(__file__).resolve().parent.parent / "app.py"
BOTH = r"Primary=localhost;Secondary=localhost\SQLDEV2"


@pytest.fixture(autouse=True)
def two_servers(monkeypatch):
    """Every UI test monitors the two real instances unless it says otherwise."""
    monkeypatch.setenv("SQL_SERVERS", BOTH)


def _run_app(page: str | None = None, server: str | None = "Primary") -> AppTest:
    at = AppTest.from_file(str(APP_FILE), default_timeout=60)
    if server:
        at.session_state["selected_server"] = server           # as if chosen via search
    at.run()
    assert not at.exception, at.exception
    if page:
        at.switch_page(page).run()
        assert not at.exception, at.exception
    return at


def _text(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown)


# --- Search-first home page -------------------------------------------------------------

def test_home_shows_no_server_until_searched():
    at = _run_app(server=None)
    assert at.title[0].value == "Find a Server"
    assert len(at.metric) == 0 and len(at.dataframe) == 0     # nothing about any server
    assert "Primary" not in _text(at) and "SQLDEV2" not in _text(at)
    assert any("Type a server name" in i.value for i in at.info)


def test_search_with_one_match_shows_only_that_server():
    at = _run_app(server=None)
    at.text_input(key="srv_search").set_value("sqldev2").run()
    assert not at.exception, at.exception
    assert at.session_state["selected_server"] == "Secondary"
    text = _text(at)
    assert "Secondary" in text and "localhost\\SQLDEV2" in text
    assert "Primary" not in text                               # the other server is not shown
    assert "Blocked sessions" in [m.label for m in at.metric]


def test_search_with_several_matches_lists_names_only_then_select():
    at = _run_app(server=None)
    at.text_input(key="srv_search").set_value("local").run()   # both connection names match
    assert len(at.metric) == 0                                 # names only, no data yet
    assert {b.key for b in at.button} >= {"pick_Primary", "pick_Secondary"}
    at.button(key="pick_Primary").click().run()
    assert not at.exception, at.exception
    assert at.session_state["selected_server"] == "Primary"
    assert "Blocked sessions" in [m.label for m in at.metric]


def test_search_primary_after_secondary_switches_server():
    at = _run_app(server=None)
    at.text_input(key="srv_search").set_value("sqldev2").run()
    assert at.session_state["selected_server"] == "Secondary"
    at.text_input(key="srv_search").set_value("primary")
    at.button(key="srv_search_go").click().run()               # the Search button applies it
    assert not at.exception, at.exception
    assert at.session_state["selected_server"] == "Primary"
    text = _text(at)
    assert "Showing: Primary" in text and "SQLDEV2" not in text      # main page AND sidebar
    assert "<div class='srv-name'>Primary</div>" in " ".join(m.value for m in at.sidebar.markdown)


def test_search_by_real_sql_server_name():
    from monitor.config import local_computer_name
    machine = local_computer_name()                                  # e.g. SQLHOST-01
    at = _run_app(server=None)
    at.text_input(key="srv_search").set_value(machine).run()
    assert not at.exception, at.exception
    assert at.session_state["selected_server"] == "Primary"        # exact name opens directly
    assert f"`{machine}`" in _text(at)                              # real SQL Server name shown
    at.text_input(key="srv_search").set_value(machine.lower() + "\\sqldev2").run()
    assert at.session_state["selected_server"] == "Secondary"


def test_search_without_match_says_so():
    at = _run_app(server=None)
    at.text_input(key="srv_search").set_value("PAYROLL-PROD-99").run()
    assert any("No server matches" in w.value for w in at.warning)
    assert len(at.metric) == 0


def test_module_page_without_selection_asks_to_search():
    at = _run_app("views/health.py", server=None)
    assert any("No server selected" in i.value for i in at.info)
    assert len(at.metric) == 0


def test_sidebar_does_not_list_all_servers():
    at = _run_app("views/health.py", server="Secondary")
    sidebar_text = " ".join(m.value for m in at.sidebar.markdown)
    assert "Secondary" in sidebar_text and "Primary" not in sidebar_text
    assert len(at.sidebar.selectbox) == 0                      # no all-servers dropdown
    # the chosen server comes FIRST in the sidebar, the page menu after it
    assert sidebar_text.index("srv-name") < sidebar_text.index("Live monitoring")


# --- Module 1 page ---------------------------------------------------------------------

def test_dashboard_renders_connected_state():
    at = _run_app("views/health.py")
    assert at.title[0].value == "SQL Server Health"
    assert any("ONLINE" in s.value for s in at.success)
    labels = [m.label for m in at.metric]
    for expected in ("Uptime", "DBs online", "Total size", "SQL memory"):
        assert expected in labels
    assert len(at.dataframe) == 1
    assert "showing <b>Primary</b>" in _text(at)


def test_search_filter_narrows_table():
    at = _run_app("views/health.py")
    at.text_input[0].set_value("ms").run()
    assert list(at.dataframe[0].value["database_name"]) == ["msdb"]


def test_hide_system_databases_leaves_only_user_databases():
    at = _run_app("views/health.py")
    at.toggle[0].set_value(False).run()
    system = {"master", "model", "msdb", "tempdb"}
    if len(at.dataframe):
        names = set(at.dataframe[0].value["database_name"])
        assert names and not (names & system)
    else:
        assert any("No databases match" in i.value for i in at.info)


def test_status_filter_critical_is_empty_when_all_online():
    at = _run_app("views/health.py")
    at.selectbox(key="health_status").select("Critical").run()
    assert any("No databases match" in i.value for i in at.info)


def test_every_page_follows_the_chosen_server():
    at = _run_app("views/health.py", server="Secondary")
    assert "showing <b>Secondary</b>" in _text(at)
    for page in ("views/query_performance.py", "views/sessions_blocking.py", "views/backups.py"):
        at.switch_page(page).run()
        assert not at.exception, (page, at.exception)
        assert "showing <b>Secondary</b>" in _text(at), page


def test_tc02_unreachable_server_isolated(monkeypatch):
    monkeypatch.setenv("SQL_SERVERS", "Primary=localhost;Offline demo=no-such-server.invalid")
    monkeypatch.setenv("SQL_LOGIN_TIMEOUT", "3")
    st.cache_data.clear()
    at = _run_app(server=None)
    at.text_input(key="srv_search").set_value("offline").run()
    assert not at.exception, at.exception
    assert any("is unavailable" in e.value for e in at.error)
    at.switch_page("views/health.py").run()
    assert any("is unavailable" in e.value for e in at.error)
    assert len(at.metric) == 0                                 # no stale numbers
    # the healthy server is unaffected
    good = _run_app("views/health.py", server="Primary")
    assert any("ONLINE" in s.value for s in good.success)
    st.cache_data.clear()


# --- Modules 2-5 pages -------------------------------------------------------------------

def test_query_performance_page_renders():
    at = _run_app("views/query_performance.py")
    assert at.title[0].value == "Query Performance"
    assert any("plan cache" in i.value for i in at.info)
    assert len(at.dataframe) == 1 or any("No queries match" in i.value for i in at.info)


def test_query_performance_rank_by_every_metric():
    at = _run_app("views/query_performance.py")
    for option in at.selectbox(key="qp_metric").options:
        at.selectbox(key="qp_metric").select(option).run()
        assert not at.exception, (option, at.exception)


def test_sessions_page_renders():
    at = _run_app("views/sessions_blocking.py")
    assert at.title[0].value == "Sessions & Blocking"
    banners = [s.value for s in at.success] + [e.value for e in at.error]
    assert any("blocking" in b.lower() for b in banners)
    for toggle in at.toggle:
        toggle.set_value(not toggle.value).run()
        assert not at.exception, at.exception


def test_backups_page_renders_and_policy_is_adjustable():
    at = _run_app("views/backups.py")
    assert at.title[0].value == "Backup Monitoring"
    assert any("not a verified restore" in w.value for w in at.warning)
    at.number_input(key="pol_log").set_value(1).run()
    assert not at.exception, at.exception
    assert any("adjusted policy" in i.value for i in at.info)


def test_alerts_page_shows_only_chosen_server_and_no_duplicates():
    at = _run_app("views/alerts.py", server="Primary")
    assert at.title[0].value == "Alerts & DBA Recommendations"
    first = next(m.value for m in at.metric if m.label == "Active")
    assert "Affected: **Secondary" not in _text(at)
    for _ in range(3):
        at.run()
    assert not at.exception, at.exception
    assert next(m.value for m in at.metric if m.label == "Active") == first
    at.toggle(key="al_all").set_value(True).run()                # opt-in: every server
    assert not at.exception, at.exception
