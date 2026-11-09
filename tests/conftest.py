"""
Shared test setup.

Every test gets its own temporary alert-store file, so tests never write into
the real alert history (%USERPROFILE%\\CapstoneMonitor\\alerts.db), and the
Streamlit data cache is cleared so tests do not see each other's results.
"""

import pytest
import streamlit as st


@pytest.fixture(autouse=True)
def isolated_alert_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ALERT_STORE_PATH", str(tmp_path / "alerts_test.db"))
    st.cache_data.clear()
    yield
    st.cache_data.clear()
