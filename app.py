"""
Database Server Monitoring and Performance Analysis System - entry point.

This file only sets up the page, the sidebar (brand, chosen server, menu) and
the connection indicator. Each monitoring module is its own page in views/.

Data flow:  SQL Server DMVs -> monitor/db.py (pyodbc) -> monitor/collectors.py
            -> views/*.py (Streamlit cards, Plotly charts, tables)

Start with:  .\\run_app.ps1     then open http://dbmonitor.localhost
"""

import streamlit as st

st.set_page_config(page_title="SQL Server Monitor", page_icon="🗄️",
                   layout="wide", initial_sidebar_state="expanded")

from monitor import ui  # noqa: E402  (import after set_page_config)

ui.inject_css()

PAGES = {
    "search": st.Page("views/server_search.py", title="Find a server", icon=":material/search:", default=True),
    "health": st.Page("views/health.py", title="Server health", icon=":material/monitor_heart:"),
    "queries": st.Page("views/query_performance.py", title="Query performance", icon=":material/bolt:"),
    "sessions": st.Page("views/sessions_blocking.py", title="Sessions & blocking", icon=":material/lock_clock:"),
    "backups": st.Page("views/backups.py", title="Backups", icon=":material/backup:"),
    "alerts": st.Page("views/alerts.py", title="Alerts & advice", icon=":material/notifications:"),
    "history": st.Page("views/history.py", title="Performance history", icon=":material/insights:"),
}
SECTIONS = {
    "Overview": [PAGES["search"]],
    "Live monitoring": [PAGES[k] for k in ("health", "queries", "sessions", "backups", "alerts")],
    "Trend analysis": [PAGES["history"]],
}

# Streamlit's own menu would always sit at the very top of the sidebar; it is hidden
# and drawn by ui.render_sidebar() BELOW the chosen server instead.
navigation = st.navigation(list(PAGES.values()), position="hidden")
if ui.demo_mode():
    from monitor.demo_data import DEMO_NOTICE
    st.warning(f"**{DEMO_NOTICE}** All servers, numbers and alerts on these pages are fictional "
               "examples that show how the dashboard works. Try searching for **prod**.", icon="🧪")
ui.render_sidebar(SECTIONS, on_search_page=navigation.title == PAGES["search"].title)
navigation.run()
