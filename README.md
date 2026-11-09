# Database Server Monitoring and Performance Analysis System

A lightweight, web-based SQL Server monitoring dashboard built with **Python,
Streamlit, pyodbc, pandas and Plotly**. Final-year college capstone project,
inspired by the open-source [SQLMonitor](https://github.com/imajaydwivedi/SQLMonitor)
project (MIT) - see [LICENSE_ATTRIBUTION.md](LICENSE_ATTRIBUTION.md).

```
SQL Server (DMVs, catalog views, msdb)
        │  read-only SELECTs, Windows Authentication
        ▼
monitor/db.py  (pyodbc)  →  monitor/collectors.py  →  app.py (Streamlit + Plotly)
                                                        http://dbmonitor.localhost
```

## Feature status

| # | Module | Status |
|---|---|---|
| 1 | SQL Server health (connectivity, version, uptime, databases, sizes, log usage) | **Implemented · 33 tests passing (unit, integration, headless UI) on 2026-11-09** |
| 2 | Query performance analyser (`sys.dm_exec_query_stats`) | **Implemented · 51 tests passing · verified with CapstoneDemo workload (2,382 calls) on 2026-11-09** |
| 3 | Active sessions & blocking | **Implemented · tested incl. live blocking (TC-06) · demo chain 68 → 69 → 70 detected and auto-released on 2026-11-09** |
| 4 | Backup monitoring (`msdb.dbo.backupset`) | **Implemented · 77 tests passing · verified with real CapstoneDemo full/diff/log backups + RESTORE VERIFYONLY on 2026-11-09** |
| 5 | Alerts & DBA recommendations | **Implemented · 92 tests passing · live de-duplication verified (2 alerts, 3 checks, no duplicates) on 2026-11-09** |
| + | Multi-instance monitoring (search-first "Find a server", one server at a time, alerts per server) | **Implemented · 110 tests passing against both instances on 2026-11-09** |

## Public demo (sample data only)

Set `DEMO_MODE=true` to run the dashboard with **five fictional servers and
generated SAMPLE data** - every page shows a "Demo mode - SAMPLE data" banner and
the app **never connects to SQL Server** (enforced in `monitor/db.py`, checked by
`tests/test_demo_mode.py`). The sample data goes through the same health, ranking,
blocking-chain, backup-policy and alert logic as real data, so reviewers see the
real behaviour of the interface without any real server data.

```powershell
$env:DEMO_MODE = 'true'; C:\path\to\venv\Scripts\python.exe -m streamlit run app.py
```

For a public link, deploy this repository on **Streamlit Community Cloud**
(share.streamlit.io): main file `app.py`, and in *Advanced settings → Secrets* add
`DEMO_MODE = "true"`. `packages.txt` installs the ODBC driver manager so `pyodbc`
can be imported there (it is never used in demo mode).

## Monitored servers

Set in `.env` as `SQL_SERVERS=Primary=localhost;Secondary=localhost\SQLDEV2`
(`FriendlyName=connection name`, separated by `;`).

**Search-first, one server at a time** (built for large fleets): the home page
**Find a server** shows only a search box. Type part of a name - friendly name
(`Primary`), connection name (`localhost\SQLDEV2`), instance (`SQLDEV2`) or the real
SQL Server / computer name (`SQLHOST-01`) - and press **Enter** or click
**Search**: an exact name or a single match opens that server directly; several
matches list names only, to pick one. Every page then shows **only the chosen server** (also via
`?server=Primary` in the address bar); the sidebar shows that server and a
*Change server* link - there is no list of all servers. Searching only reads the
server list, so it stays instant with thousands of entries. Alerts are still
evaluated for every server; the Alerts page shows the chosen server unless
*Show alerts from all servers* is switched on. An unreachable server shows a
clear error without affecting the others.

| Name | Connection | Notes |
|---|---|---|
| Primary | `localhost` | Default instance, max memory 2 GB, CapstoneDemo test DB (FULL recovery) |
| Secondary | `localhost\SQLDEV2` | Named instance, max memory 1 GB, SalesArchive test DB (SIMPLE recovery), all databases backed up |

## Folder structure

| Path | What it is |
|---|---|
| `app.py` | Entry point: sidebar menu, chosen server, connection indicator |
| `views/server_search.py` | Home page - search for a server, then see only that server |
| `views/health.py` | Module 1 page - server health |
| `views/query_performance.py` | Module 2 page - top queries from the plan cache |
| `views/sessions_blocking.py` | Module 3 page - sessions, blocking chains, waits (never kills sessions) |
| `views/backups.py` | Module 4 page - last full/diff/log per database, overdue rules, backup history |
| `views/alerts.py` | Module 5 page - active / resolved alerts with DBA investigation steps |
| `monitor/alerts.py` | Alert rules, runbook text, de-duplicating alert store (SQLite) |
| `tools/blocking_demo.py` | Self-releasing 3-session blocking demo (A → B → C, always ROLLBACK) |
| `monitor/ui.py` | Shared colours, chart style, cached loaders, sidebar |
| `sql/demo/` | Primary: `01_create_capstone_demo.sql`, `02_create_blocking_demo_table.sql`, `03_backup_capstone_demo.sql`, `04_backup_capstone_log.sql`, `99_drop_capstone_demo.sql` · Secondary (refuse to run elsewhere): `05_create_sqldev2_salesarchive.sql`, `06_backup_sqldev2.sql`, `99_drop_sqldev2_salesarchive.sql` |
| `sql/admin/` | `01_set_min_server_memory.sql` (512 MB floor, see Notes) |
| `tools/run_sql_script.py` | Runs a `.sql` file like SSMS F5 (splits on `GO`, stops on first error); `--server localhost\SQLDEV2` for the second instance |
| `tools/generate_query_workload.py` | Read-only demo workload against CapstoneDemo |
| `monitor/config.py` | Reads settings from `.env` |
| `monitor/db.py` | SQL Server connection, query runner, friendly error messages |
| `monitor/queries.py` | All monitoring T-SQL |
| `monitor/collectors.py` | Runs queries, adds health status, formats values |
| `tests/` | Unit tests (no SQL Server needed), integration tests (real instances), `test_app_ui.py` (dashboard run headlessly), `test_multiserver.py` (both instances) |
| `.env.example` | Settings template - copy to `.env` |
| `.streamlit/config.toml` | Dashboard theme; listens on localhost only |
| `run_app.ps1` | Starts the dashboard |

## Environment (development PC)

- Windows 11 Enterprise, Python 3.13.17
- Virtual environment: `%USERPROFILE%\venvs\capstone` (outside OneDrive on purpose)
- SQL Server 2022 Developer Edition CU27 + GDR (16.0.4305.5), two instances
  (`localhost`, `localhost\SQLDEV2`), Windows Authentication only, Shared Memory
  (no TCP listener, no SQL Browser), max server memory 2 GB / 1 GB.
  Both services are *Automatic (Delayed Start)*: after a PC restart allow a few minutes.
- Microsoft ODBC Driver 17 for SQL Server

## How to run

**PowerShell**, in this folder:

```powershell
.\run_app.ps1
```

Open **http://dbmonitor.localhost** in Edge or Chrome. Stop with **Ctrl+C**.

The dashboard listens on port 80 of this PC only (loopback - not reachable from
other computers). Edge and Chrome send every `*.localhost` name to this PC by
themselves, so no DNS, hosts-file or admin change is needed. Port and address are
set in `run_app.ps1` (not in `.streamlit/config.toml`, so hosting services keep their
own defaults). If port 80 is taken by another program,
`run_app.ps1` says so; `http://127.0.0.1` also works.

*Production note (future enhancement):* a shared link for other users would run
the dashboard on a server behind IIS/Nginx with an HTTPS certificate, a DNS name
such as `https://dbmonitor.<company>.com`, a firewall rule and company sign-in
(Microsoft Entra ID) - all of which need IT.

First-time setup on a new PC:

```powershell
python -m venv "$env:USERPROFILE\venvs\capstone"
& "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" -m pip install -r requirements.txt
Copy-Item .env.example .env
```

## How to test

**PowerShell**, in this folder:

```powershell
# All tests (SQL Server must be running for the integration tests)
& "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" -m pytest -v

# Only the tests that do not need SQL Server
& "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" -m pytest -v -m "not integration"

# The live blocking test (holds a lock in tempdb for ~2 s; excluded by default)
& "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" -m pytest -v -m blocking
```

## Demonstration scripts

```powershell
# Query activity for Module 2 (read-only, 60 s)
& "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" tools\generate_query_workload.py --seconds 60

# Blocking for Module 3 (A → B → C for 90 s, then automatic ROLLBACK; Ctrl+C releases early)
& "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" tools\blocking_demo.py --seconds 90

# Backups for Module 4 - one log backup of CapstoneDemo (brings an overdue log back within policy)
& "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" tools\run_sql_script.py sql\demo\04_backup_capstone_log.sql
```

**Overdue-backup demo without fake data:** on the Backups page open *Backup policy*, set
"Log backup at least every (minutes)" to 1, wait two minutes → CapstoneDemo shows
"Log backup overdue". Run `04_backup_capstone_log.sql` and press *Refresh now* → healthy again.

## Permissions

The app only runs read-only SELECTs. A non-sysadmin monitoring login needs:

```sql
-- SSMS, connected as sysadmin (example for a Windows login)
CREATE LOGIN [DOMAIN\monitor_user] FROM WINDOWS;
GRANT VIEW SERVER STATE TO [DOMAIN\monitor_user];              -- DMVs (SQL 2019)
GRANT VIEW SERVER PERFORMANCE STATE TO [DOMAIN\monitor_user];  -- DMVs (SQL 2022+)
GRANT VIEW ANY DEFINITION TO [DOMAIN\monitor_user];            -- catalog metadata
-- Backup history (Module 4): read access to msdb
USE msdb; CREATE USER [DOMAIN\monitor_user]; ALTER ROLE db_datareader ADD MEMBER [DOMAIN\monitor_user];
```

## Notes

- **Clock difference:** on this Cloud PC the SQL Server service runs on UTC while
  the Windows session shows IST. All ages and uptime are calculated inside
  SQL Server (`SYSDATETIME()`), so they are always consistent.
- `tempdb` size comes from `sys.master_files` (its configured start-up size).
- **Alert history store:** the only data this app writes. A small SQLite file at
  `%USERPROFILE%\CapstoneMonitor\alerts.db` (outside OneDrive and outside AppData,
  which Windows redirects for programs started from packaged apps; change with
  `ALERT_STORE_PATH`). One row per alert key - refreshes update *last seen*,
  cleared conditions become *Resolved*, resolved alerts older than
  `ALERT_HISTORY_DAYS` (7) are deleted automatically. Delete the file to reset.
  Rules run at most once per `DASHBOARD_CACHE_SECONDS` (30 s).
- Monitoring data itself is **live only** (no metric history is stored).
