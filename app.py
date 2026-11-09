"""
Database Server Monitoring and Performance Analysis System - entry point.

This file only sets up the page, the sidebar menu and the connection indicator.
Each monitoring module is its own page in the views/ folder.

Data flow:  SQL Server DMVs -> monitor/db.py (pyodbc) -> monitor/collectors.py
            -> views/*.py (Streamlit cards, Plotly charts, tables)

Start with:  .\\run_app.ps1     then open http://dbmonitor.localhost
"""

import streamlit as st

st.set_page_config(page_title="SQL Server Monitor", page_icon="🗄️",
                   layout="wide", initial_sidebar_state="expanded")

from monitor import ui  # noqa: E402  (import after set_page_config)

ui.inject_css()

pages = [
    st.Page("views/server_search.py", title="Find a server", icon="🔎", default=True),
    st.Page("views/health.py", title="1 · Server health", icon="🩺"),
    st.Page("views/query_performance.py", title="2 · Query performance", icon="⚡"),
    st.Page("views/sessions_blocking.py", title="3 · Sessions & blocking", icon="🔒"),
    st.Page("views/backups.py", title="4 · Backups", icon="💾"),
    st.Page("views/alerts.py", title="5 · Alerts & advice", icon="🔔"),
]

with st.sidebar:
    st.markdown("### 🗄️ DB Monitor")

navigation = st.navigation(pages, position="sidebar")
if ui.demo_mode():
    from monitor.demo_data import DEMO_NOTICE
    st.warning(f"**{DEMO_NOTICE}** All servers, numbers and alerts on these pages are fictional "
               "examples that show how the dashboard works. Try searching for **prod**.", icon="🧪")
ui.render_sidebar_status()
navigation.run()
