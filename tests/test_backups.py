"""
Module 4 tests - backup monitoring.

TC-07  Backup history detection (msdb read, per-database last full/diff/log)
TC-08  Overdue-backup warnings (policy rules, recovery-model handling)
"""

from __future__ import annotations

import pandas as pd
import pytest

from monitor.collectors import (BackupPolicy, collect_backups, evaluate_backups,
                                format_age_minutes, summarize_backups)

POLICY = BackupPolicy(full_max_hours=168, data_max_hours=24, log_max_minutes=60)
H, D = 60, 1440                       # minutes in an hour / a day


def _db(name, model="FULL", state="ONLINE", full=None, diff=None, log=None, reuse="NOTHING"):
    return {"database_name": name, "state_desc": state, "recovery_model_desc": model,
            "log_reuse_wait_desc": reuse, "is_system": 0, "last_full_size_mb": None,
            "full_age_minutes": full, "diff_age_minutes": diff, "log_age_minutes": log}


def _eval(*rows) -> pd.DataFrame:
    return evaluate_backups(pd.DataFrame(list(rows)), POLICY).set_index("database_name")


def test_no_full_backup_is_critical_and_explains_full_recovery():
    df = _eval(_db("NeverFull", "FULL"), _db("NeverSimple", "SIMPLE"))
    assert df.loc["NeverFull", "backup_status"] == "Critical"
    assert "No recorded full backup" in df.loc["NeverFull", "findings"]
    assert "log chain" in df.loc["NeverFull", "findings"]            # pseudo-simple explained
    assert df.loc["NeverFull", "log_backup_state"] == "Chain not started"
    assert df.loc["NeverSimple", "backup_status"] == "Critical"
    assert df.loc["NeverSimple", "log_backup_state"] == "Not applicable (SIMPLE)"


def test_healthy_full_recovery_with_recent_log():
    df = _eval(_db("Good", "FULL", full=2 * H, diff=30, log=10))
    assert df.loc["Good", "backup_status"] == "Healthy"
    assert df.loc["Good", "findings"] == "Within policy"
    assert df.loc["Good", "data_age_minutes"] == 30                  # newest of full/diff


def test_simple_recovery_needs_no_log_backup():
    df = _eval(_db("Simple", "SIMPLE", full=3 * H, log=None))
    assert df.loc["Simple", "backup_status"] == "Healthy"
    assert df.loc["Simple", "log_backup_state"] == "Not applicable (SIMPLE)"


def test_old_full_backup_is_critical():
    df = _eval(_db("OldFull", "SIMPLE", full=10 * D, diff=2 * H))
    assert df.loc["OldFull", "backup_status"] == "Critical"
    assert "Last full backup 10 d 0 h ago" in df.loc["OldFull", "findings"]


def test_stale_full_and_diff_is_overdue_warning():
    df = _eval(_db("NoDiff", "SIMPLE", full=2 * D))
    assert df.loc["NoDiff", "backup_status"] == "Warning"
    assert "full/differential" in df.loc["NoDiff", "findings"]


def test_log_backup_overdue_and_no_log_since_full():
    df = _eval(_db("LogLate", "FULL", full=3 * H, log=90),
               _db("NoLogYet", "FULL", full=2 * H, log=None),
               _db("FreshFull", "FULL", full=5, log=None))
    assert df.loc["LogLate", "backup_status"] == "Warning"
    assert "Log backup overdue" in df.loc["LogLate", "findings"]
    assert df.loc["NoLogYet", "backup_status"] == "Warning"
    assert "No log backup since the full backup" in df.loc["NoLogYet", "findings"]
    assert df.loc["FreshFull", "backup_status"] == "Healthy"          # within the 60-min window


def test_differential_older_than_full_is_ignored():
    df = _eval(_db("OldDiff", "SIMPLE", full=30 * H, diff=40 * H))
    assert df.loc["OldDiff", "data_age_minutes"] == 30 * H            # stale diff base ignored
    assert df.loc["OldDiff", "backup_status"] == "Warning"


def test_offline_database_is_not_checked():
    df = _eval(_db("Off", "FULL", state="OFFLINE"))
    assert df.loc["Off", "backup_status"] == "Not checked"


def test_log_reuse_wait_hint():
    df = _eval(_db("Waiting", "FULL", full=H, log=10, reuse="LOG_BACKUP"))
    assert "LOG_BACKUP" in df.loc["Waiting", "findings"]


def test_tighter_policy_turns_healthy_into_overdue():
    row = _db("Demo", "FULL", full=20, log=5)
    assert _eval(row).loc["Demo", "backup_status"] == "Healthy"
    tight = evaluate_backups(pd.DataFrame([row]), BackupPolicy(168, 24, 2)).iloc[0]
    assert tight["backup_status"] == "Warning"                        # log older than 2 min


def test_summary_counts():
    df = evaluate_backups(pd.DataFrame([_db("A", full=H, log=5), _db("B"), _db("C", "SIMPLE", full=2 * D)]),
                          POLICY)
    s = summarize_backups(df)
    assert (s["databases"], s["healthy"], s["warning"], s["critical"]) == (3, 1, 1, 1)
    assert s["newest_age"] == 5


@pytest.mark.parametrize("minutes, text", [
    (None, "never"), (0, "< 1 min"), (45, "45 min"), (75, "1 h 15 min"), (3000, "2 d 2 h"),
])
def test_format_age_minutes(minutes, text):
    assert format_age_minutes(minutes) == text


# --- Integration ------------------------------------------------------------------

@pytest.mark.integration
def test_tc07_backup_facts_from_real_msdb():
    snap = collect_backups()
    assert snap.connected, snap.error
    names = set(snap.databases["database_name"])
    assert {"master", "model", "msdb"} <= names
    assert "tempdb" not in names                                        # cannot be backed up
    evaluated = evaluate_backups(snap.databases, POLICY)
    assert set(evaluated["backup_status"]) <= {"Healthy", "Warning", "Critical", "Not checked"}
    for col in ("backup_type", "backup_finish_date", "physical_device_name"):
        assert col in snap.history.columns or snap.history.empty
