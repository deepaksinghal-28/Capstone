"""
Shared test setup.

Every test gets its own temporary alert-store and history files, so tests never
touch the real alert history or performance history
(%USERPROFILE%\\CapstoneMonitor\\alerts.db / history.db), and the
Streamlit data cache is cleared so tests do not see each other's results.
"""

import pytest
import streamlit as st


@pytest.fixture(autouse=True)
def isolated_alert_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ALERT_STORE_PATH", str(tmp_path / "alerts_test.db"))
    monkeypatch.setenv("HISTORY_STORE_PATH", str(tmp_path / "history_test.db"))   # never the real history
    st.cache_data.clear()
    yield
    st.cache_data.clear()
