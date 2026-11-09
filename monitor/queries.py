"""
All monitoring T-SQL in one place - like a folder of saved .sql scripts in SSMS.

Rules for every query in this file:
  * Read-only SELECTs against DMVs, catalog views and msdb. No DML/DDL.
  * Times are computed INSIDE SQL Server (SYSDATETIME / SYSUTCDATETIME) so ages
    and uptime are correct even when the SQL Server service clock (UTC on this
    Cloud PC) differs from the user's Windows session clock (IST).
  * Verified against SQL Server 2022 (16.x). Required permission:
    VIEW SERVER STATE (SQL 2019) or VIEW SERVER PERFORMANCE STATE (SQL 2022+);
    a sysadmin already has it.

Idea sources: query shapes are inspired by the open-source SQLMonitor project
(MIT License, (c) Ajay Dwivedi) - see LICENSE_ATTRIBUTION.md. The queries below
were written for this capstone.
"""

# ---------------------------------------------------------------------------
# Module 1 - SQL Server health
# ---------------------------------------------------------------------------

# One row describing the instance.
#   sys.dm_os_sys_info      -> start time, CPU count, physical memory
#   sys.dm_os_process_memory-> memory the SQL Server process is using now
#   sys.configurations      -> 'max server memory (MB)'
SERVER_INFO = """
SELECT
    CAST(SERVERPROPERTY('ServerName')             AS nvarchar(256)) AS server_name,
    CAST(SERVERPROPERTY('MachineName')            AS nvarchar(256)) AS machine_name,
    CAST(SERVERPROPERTY('Edition')                AS nvarchar(256)) AS edition,
    CAST(SERVERPROPERTY('ProductVersion')         AS nvarchar(64))  AS product_version,
    CAST(SERVERPROPERTY('ProductLevel')           AS nvarchar(64))  AS product_level,
    CAST(SERVERPROPERTY('ProductUpdateLevel')     AS nvarchar(64))  AS product_update_level,
    CAST(SERVERPROPERTY('ProductUpdateReference') AS nvarchar(64))  AS product_update_reference,
    CAST(SERVERPROPERTY('Collation')              AS nvarchar(128)) AS server_collation,
    CAST(SERVERPROPERTY('IsClustered')            AS int)           AS is_clustered,
    CAST(SERVERPROPERTY('IsHadrEnabled')          AS int)           AS is_hadr_enabled,
    @@VERSION                                                       AS version_string,
    si.sqlserver_start_time,
    DATEDIFF(SECOND, si.sqlserver_start_time, SYSDATETIME())        AS uptime_seconds,
    si.cpu_count,
    si.physical_memory_kb / 1024                                    AS physical_memory_mb,
    (SELECT CAST(c.value_in_use AS int)
       FROM sys.configurations AS c
      WHERE c.name = N'max server memory (MB)')                     AS max_server_memory_mb,
    (SELECT pm.physical_memory_in_use_kb / 1024
       FROM sys.dm_os_process_memory AS pm)                         AS sql_memory_in_use_mb,
    SYSDATETIME()                                                   AS server_time,
    DATEDIFF(MINUTE, SYSUTCDATETIME(), SYSDATETIME())               AS server_utc_offset_minutes
FROM sys.dm_os_sys_info AS si;
"""

# One row per database with state, recovery model, file sizes and log usage.
#   sys.databases            -> state (ONLINE/OFFLINE/RESTORING/...), recovery model
#   sys.master_files         -> file sizes in 8 KB pages (data = type 0, log = type 1)
#   sys.dm_os_performance_counters 'Percent Log Used' -> current log fullness
# Note: for tempdb, sys.master_files shows the configured (startup) size.
DATABASES = """
WITH file_sizes AS (
    SELECT
        mf.database_id,
        SUM(CASE WHEN mf.type = 0 THEN CAST(mf.size AS bigint) ELSE 0 END) * 8 / 1024.0 AS data_size_mb,
        SUM(CASE WHEN mf.type = 1 THEN CAST(mf.size AS bigint) ELSE 0 END) * 8 / 1024.0 AS log_size_mb,
        SUM(CAST(mf.size AS bigint)) * 8 / 1024.0                                         AS total_size_mb,
        COUNT(*)                                                                          AS file_count
    FROM sys.master_files AS mf
    GROUP BY mf.database_id
),
log_used AS (
    SELECT RTRIM(pc.instance_name) AS database_name,
           CAST(pc.cntr_value AS int) AS log_used_pct
    FROM sys.dm_os_performance_counters AS pc
    WHERE pc.object_name LIKE N'%:Databases%'
      AND pc.counter_name = N'Percent Log Used'
)
SELECT
    d.database_id,
    d.name                                       AS database_name,
    d.state_desc,
    d.user_access_desc,
    d.recovery_model_desc,
    d.compatibility_level,
    CAST(d.is_read_only AS int)                  AS is_read_only,
    CASE WHEN d.database_id <= 4 THEN 1 ELSE 0 END AS is_system,
    d.create_date,
    CAST(fs.data_size_mb  AS decimal(18, 2))     AS data_size_mb,
    CAST(fs.log_size_mb   AS decimal(18, 2))     AS log_size_mb,
    CAST(fs.total_size_mb AS decimal(18, 2))     AS total_size_mb,
    fs.file_count,
    lu.log_used_pct
FROM sys.databases AS d
LEFT JOIN file_sizes AS fs ON fs.database_id = d.database_id
LEFT JOIN log_used   AS lu ON lu.database_name = d.name
ORDER BY d.database_id;
"""

# ---------------------------------------------------------------------------
# Module 2 - Query performance (plan cache statistics)
# ---------------------------------------------------------------------------
#
# sys.dm_exec_query_stats keeps one row per cached statement plan, with totals
# accumulated SINCE THAT PLAN WAS CACHED. It is not a full history: rows vanish
# when a plan is evicted (memory pressure, recompile, DBCC FREEPROCCACHE,
# ALTER DATABASE, restart). Statements run with OPTION(RECOMPILE) never appear.
#
# Units in the DMV: *_worker_time and *_elapsed_time are MICROSECONDS;
# *_logical_reads are 8 KB pages. Python converts times to milliseconds.
#
# Rows are grouped by query_hash so the same query shape with different
# literal values (ad-hoc SQL) is counted once. For the text / database we take
# the most CPU-expensive cached plan of that shape as the representative.
#
# Parameters (pyodbc '?'): 1) how many rows, 2) sort metric:
#   'cpu' | 'duration' | 'reads' | 'executions' | 'avg_cpu' | 'avg_duration'
TOP_QUERIES = """
DECLARE @top int = ?, @metric varchar(20) = ?;

WITH by_shape AS (
    SELECT
        qs.query_hash,
        SUM(qs.execution_count)        AS execution_count,
        SUM(qs.total_worker_time)      AS total_cpu_us,
        SUM(qs.total_elapsed_time)     AS total_elapsed_us,
        SUM(qs.total_logical_reads)    AS total_logical_reads,
        SUM(qs.total_logical_writes)   AS total_logical_writes,
        SUM(qs.total_rows)             AS total_rows,
        MAX(qs.max_worker_time)        AS max_cpu_us,
        MAX(qs.max_elapsed_time)       AS max_elapsed_us,
        MIN(qs.creation_time)          AS first_cached_time,
        MAX(qs.last_execution_time)    AS last_execution_time,
        COUNT(*)                       AS cached_plan_count
    FROM sys.dm_exec_query_stats AS qs
    GROUP BY qs.query_hash
),
ranked AS (
    SELECT TOP (@top) *
    FROM by_shape
    ORDER BY
        CASE @metric
            WHEN 'cpu'          THEN CAST(total_cpu_us        AS float)
            WHEN 'duration'     THEN CAST(total_elapsed_us    AS float)
            WHEN 'reads'        THEN CAST(total_logical_reads AS float)
            WHEN 'executions'   THEN CAST(execution_count     AS float)
            WHEN 'avg_cpu'      THEN CAST(total_cpu_us     AS float) / NULLIF(execution_count, 0)
            WHEN 'avg_duration' THEN CAST(total_elapsed_us AS float) / NULLIF(execution_count, 0)
        END DESC
)
SELECT
    CONVERT(varchar(20), r.query_hash, 1)                     AS query_hash,
    r.execution_count,
    r.total_cpu_us,
    r.total_elapsed_us,
    r.total_logical_reads,
    r.total_logical_writes,
    r.total_rows,
    r.max_cpu_us,
    r.max_elapsed_us,
    r.first_cached_time,
    r.last_execution_time,
    DATEDIFF(SECOND, r.last_execution_time, SYSDATETIME())    AS seconds_since_last_run,
    r.cached_plan_count,
    DB_NAME(CAST(pa.value AS int))                            AS database_name,
    OBJECT_NAME(st.objectid, st.dbid)                         AS object_name,
    CASE WHEN st.text LIKE N'%/* DBMonitor */%' THEN 1 ELSE 0 END AS is_monitor_query,
    LTRIM(RTRIM(SUBSTRING(st.text,
        rep.statement_start_offset / 2 + 1,
        (CASE WHEN rep.statement_end_offset = -1
              THEN DATALENGTH(st.text)
              ELSE rep.statement_end_offset END
         - rep.statement_start_offset) / 2 + 1)))             AS query_text
FROM ranked AS r
CROSS APPLY (
    SELECT TOP (1) qs2.sql_handle, qs2.plan_handle,
                   qs2.statement_start_offset, qs2.statement_end_offset
    FROM sys.dm_exec_query_stats AS qs2
    WHERE qs2.query_hash = r.query_hash
    ORDER BY qs2.total_worker_time DESC
) AS rep
OUTER APPLY sys.dm_exec_sql_text(rep.sql_handle) AS st
OUTER APPLY (
    SELECT pa1.value
    FROM sys.dm_exec_plan_attributes(rep.plan_handle) AS pa1
    WHERE pa1.attribute = N'dbid'
) AS pa;
"""

# ---------------------------------------------------------------------------
# Module 3 - Active sessions and blocking
# ---------------------------------------------------------------------------
#
# SAFETY GUARD (SQL Server 2022 CU27 + GDR, KB5126934 known issue):
#   "Queries against sys.dm_exec_requests might raise an access violation and
#    generate a dump file if they run while a database is still recovering."
# So we first check sys.databases; if any database is RECOVERING / RESTORING /
# RECOVERY_PENDING, the sessions query is built WITHOUT sys.dm_exec_requests and
# uses only sys.dm_exec_sessions + sys.dm_os_waiting_tasks.
DATABASES_IN_RECOVERY = """
SELECT name, state_desc
FROM sys.databases
WHERE state_desc IN (N'RECOVERING', N'RESTORING', N'RECOVERY_PENDING');
"""


def sessions_query(include_requests: bool) -> str:
    """
    One row per user session (excluding the monitor's own connection).

    Blocking info comes from sys.dm_exec_requests.blocking_session_id and, as a
    fallback / for the exact wait duration, sys.dm_os_waiting_tasks.
    A blocker that is idle ("sleeping") with an open transaction has no request
    row - its last statement comes from sys.dm_exec_connections.most_recent_sql_handle.
    Units: wait / cpu / elapsed times are milliseconds.
    """
    if include_requests:
        request_columns = """
        r.status                                                AS request_status,
        r.command,
        r.cpu_time                                              AS cpu_ms,
        r.logical_reads,
        r.total_elapsed_time                                    AS elapsed_ms,
        r.start_time                                            AS request_start_time,
        CASE WHEN r.session_id IS NOT NULL THEN 1 ELSE 0 END    AS has_request,
        COALESCE(NULLIF(r.blocking_session_id, 0), wt.blocking_session_id, 0) AS blocking_session_id,
        COALESCE(r.wait_type, wt.wait_type)                     AS wait_type,
        COALESCE(wt.wait_duration_ms, r.wait_time, 0)           AS wait_ms,
        COALESCE(r.wait_resource, wt.resource_description)      AS wait_resource,
        LTRIM(RTRIM(CASE
            WHEN r.sql_handle IS NOT NULL THEN SUBSTRING(st.text, r.statement_start_offset / 2 + 1,
                 (CASE WHEN r.statement_end_offset = -1 THEN DATALENGTH(st.text)
                       ELSE r.statement_end_offset END - r.statement_start_offset) / 2 + 1)
            ELSE st.text END))                                  AS sql_text"""
        request_join = """
    LEFT JOIN sys.dm_exec_requests AS r ON r.session_id = s.session_id"""
        sql_handle = "COALESCE(r.sql_handle, c.most_recent_sql_handle)"
    else:
        request_columns = """
        CAST(NULL AS nvarchar(30))                              AS request_status,
        CAST(NULL AS nvarchar(32))                              AS command,
        CAST(NULL AS int)                                       AS cpu_ms,
        CAST(NULL AS bigint)                                    AS logical_reads,
        CAST(NULL AS int)                                       AS elapsed_ms,
        CAST(NULL AS datetime)                                  AS request_start_time,
        CASE WHEN s.status = N'running' THEN 1 ELSE 0 END       AS has_request,
        COALESCE(wt.blocking_session_id, 0)                     AS blocking_session_id,
        wt.wait_type,
        COALESCE(wt.wait_duration_ms, 0)                        AS wait_ms,
        wt.resource_description                                 AS wait_resource,
        LTRIM(RTRIM(st.text))                                   AS sql_text"""
        request_join = ""
        sql_handle = "c.most_recent_sql_handle"

    return f"""
SELECT
        s.session_id,
        s.login_name,
        s.host_name,
        s.program_name,
        DB_NAME(s.database_id)                                  AS database_name,
        s.status                                                AS session_status,
        s.open_transaction_count,
        s.login_time,
        s.last_request_start_time,
        s.last_request_end_time,
        DATEDIFF(SECOND, s.last_request_end_time, SYSDATETIME()) AS idle_seconds,
        SYSDATETIME()                                           AS server_time,{request_columns}
FROM sys.dm_exec_sessions AS s{request_join}
OUTER APPLY (
    SELECT TOP (1) c1.most_recent_sql_handle
    FROM sys.dm_exec_connections AS c1
    WHERE c1.session_id = s.session_id
) AS c
OUTER APPLY (
    SELECT TOP (1) wt1.blocking_session_id, wt1.wait_type, wt1.wait_duration_ms,
                   wt1.resource_description
    FROM sys.dm_os_waiting_tasks AS wt1
    WHERE wt1.session_id = s.session_id
      AND wt1.blocking_session_id IS NOT NULL
      AND wt1.blocking_session_id <> s.session_id
    ORDER BY wt1.wait_duration_ms DESC
) AS wt
OUTER APPLY sys.dm_exec_sql_text({sql_handle}) AS st
WHERE s.is_user_process = 1
  AND s.session_id <> @@SPID
ORDER BY s.session_id;
"""


# ---------------------------------------------------------------------------
# Module 4 - Backup monitoring (msdb backup HISTORY - not proof of restorability)
# ---------------------------------------------------------------------------
#
# msdb.dbo.backupset.type: D = full, I = differential, L = transaction log.
# History is matched to the CURRENT database by name AND database_guid
# (sys.database_recovery_status), so backups of an older, dropped database with
# the same name are not counted. Only backups taken on this server count.
# Ages are calculated inside SQL Server (backup dates are in server local time).
LAST_BACKUPS = """
WITH history AS (
    SELECT
        b.database_name,
        b.database_guid,
        b.type,
        b.backup_finish_date,
        b.is_copy_only,
        b.backup_size,
        b.compressed_backup_size,
        ROW_NUMBER() OVER (PARTITION BY b.database_name, b.database_guid, b.type
                           ORDER BY b.backup_finish_date DESC) AS rn
    FROM msdb.dbo.backupset AS b
    WHERE b.server_name = @@SERVERNAME
      AND b.type IN ('D', 'I', 'L')
)
SELECT
    d.database_id,
    d.name                                         AS database_name,
    d.state_desc,
    d.recovery_model_desc,
    d.log_reuse_wait_desc,
    CAST(d.is_read_only AS int)                    AS is_read_only,
    CASE WHEN d.database_id <= 4 THEN 1 ELSE 0 END AS is_system,
    d.create_date,
    f.backup_finish_date                           AS last_full,
    CAST(f.is_copy_only AS int)                    AS last_full_copy_only,
    CAST(f.compressed_backup_size / 1048576.0 AS decimal(18, 2)) AS last_full_size_mb,
    i.backup_finish_date                           AS last_diff,
    l.backup_finish_date                           AS last_log,
    DATEDIFF(MINUTE, f.backup_finish_date, SYSDATETIME()) AS full_age_minutes,
    DATEDIFF(MINUTE, i.backup_finish_date, SYSDATETIME()) AS diff_age_minutes,
    DATEDIFF(MINUTE, l.backup_finish_date, SYSDATETIME()) AS log_age_minutes,
    DATEDIFF(MINUTE, d.create_date, SYSDATETIME())        AS db_age_minutes,
    SYSDATETIME()                                  AS server_time
FROM sys.databases AS d
JOIN sys.database_recovery_status AS drs ON drs.database_id = d.database_id
LEFT JOIN history AS f ON f.database_name = d.name AND f.database_guid = drs.database_guid
                      AND f.type = 'D' AND f.rn = 1
LEFT JOIN history AS i ON i.database_name = d.name AND i.database_guid = drs.database_guid
                      AND i.type = 'I' AND i.rn = 1
LEFT JOIN history AS l ON l.database_name = d.name AND l.database_guid = drs.database_guid
                      AND l.type = 'L' AND l.rn = 1
WHERE d.name <> N'tempdb'            -- tempdb cannot be backed up
ORDER BY d.database_id;
"""

# Most recent individual backups (for the history table). Parameter: row count.
BACKUP_HISTORY = """
DECLARE @rows int = ?;
SELECT TOP (@rows)
    b.database_name,
    CASE b.type WHEN 'D' THEN 'Full' WHEN 'I' THEN 'Differential' WHEN 'L' THEN 'Log'
                ELSE b.type END                     AS backup_type,
    b.backup_start_date,
    b.backup_finish_date,
    DATEDIFF(SECOND, b.backup_start_date, b.backup_finish_date)     AS duration_seconds,
    DATEDIFF(MINUTE, b.backup_finish_date, SYSDATETIME())           AS age_minutes,
    CAST(b.backup_size / 1048576.0 AS decimal(18, 2))               AS size_mb,
    CAST(b.compressed_backup_size / 1048576.0 AS decimal(18, 2))    AS compressed_mb,
    CAST(b.is_copy_only AS int)                                     AS is_copy_only,
    CAST(b.has_backup_checksums AS int)                             AS has_checksum,
    b.recovery_model,
    b.user_name,
    mf.physical_device_name
FROM msdb.dbo.backupset AS b
LEFT JOIN msdb.dbo.backupmediafamily AS mf ON mf.media_set_id = b.media_set_id
WHERE b.server_name = @@SERVERNAME
ORDER BY b.backup_finish_date DESC;
"""


# ---------------------------------------------------------------------------
# Module 5 - extra input for alert rules
# ---------------------------------------------------------------------------
# Windows "low physical memory" notifications received by SQL Server in the last
# N minutes (parameter). On this Cloud PC they caused the plan cache to be
# emptied every minute, so they are worth an alert.
MEMORY_PRESSURE = """
DECLARE @window_minutes int = ?;
WITH events AS (
    SELECT rb.[timestamp],
           CAST(rb.record AS xml).value('(Record/ResourceMonitor/Notification)[1]',
                                        'varchar(60)') AS notification
    FROM sys.dm_os_ring_buffers AS rb
    WHERE rb.ring_buffer_type = N'RING_BUFFER_RESOURCE_MONITOR'
)
SELECT
    COUNT(*)                                              AS low_memory_events,
    MIN((si.ms_ticks - e.[timestamp]) / 60000)            AS minutes_since_last,
    (SELECT pm.physical_memory_in_use_kb / 1024 FROM sys.dm_os_process_memory AS pm) AS sql_memory_mb,
    (SELECT sm.available_physical_memory_kb / 1024 FROM sys.dm_os_sys_memory AS sm)  AS os_available_mb
FROM events AS e
CROSS JOIN sys.dm_os_sys_info AS si
WHERE e.notification = 'RESOURCE_MEMPHYSICAL_LOW'
  AND e.[timestamp] > si.ms_ticks - @window_minutes * 60000;
"""


# CPU history kept by SQL Server itself: one record per minute for roughly the
# last 4 hours in RING_BUFFER_SCHEDULER_MONITOR - so a trend line needs no
# extra storage. Parameter: number of minutes to return.
CPU_HISTORY = """
DECLARE @minutes int = ?;
DECLARE @now_ticks bigint = (SELECT ms_ticks FROM sys.dm_os_sys_info);
SELECT TOP (@minutes)
    DATEADD(ms, -1 * (@now_ticks - rb.[timestamp]), SYSDATETIME())                         AS event_time,
    rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int') AS sql_cpu_pct,
    100 - rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int')
        - rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int') AS other_cpu_pct
FROM (
    SELECT [timestamp], CONVERT(xml, record) AS record
    FROM sys.dm_os_ring_buffers
    WHERE ring_buffer_type = N'RING_BUFFER_SCHEDULER_MONITOR'
      AND record LIKE N'%<SystemHealth>%'
) AS rb
ORDER BY rb.[timestamp] DESC;
"""


# How much history the plan cache currently holds (for the "statistics since" note).
PLAN_CACHE_SUMMARY = """
SELECT
    COUNT(*)                                      AS cached_statements,
    COUNT(DISTINCT qs.query_hash)                 AS distinct_query_shapes,
    MIN(qs.creation_time)                         AS oldest_plan_cached,
    (SELECT sqlserver_start_time FROM sys.dm_os_sys_info) AS sqlserver_start_time,
    SYSDATETIME()                                 AS server_time
FROM sys.dm_exec_query_stats AS qs;
"""
