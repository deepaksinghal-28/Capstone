/* =============================================================================
   04_backup_capstone_log.sql
   Takes ONE transaction-log backup of the TEST database [CapstoneDemo].

   Use it during the demo to show an "overdue log backup" turning back to
   "within policy", and to let SQL Server reuse the log space.
   Requires a previous full backup (03_backup_capstone_demo.sql).
   Files go to the default backup folder with a time-stamped name.

   HOW TO RUN
     & "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" tools\run_sql_script.py sql\demo\04_backup_capstone_log.sql
   ============================================================================= */
SET NOCOUNT ON;
GO

DECLARE @dir nvarchar(4000) = CAST(SERVERPROPERTY('InstanceDefaultBackupPath') AS nvarchar(4000));
IF RIGHT(@dir, 1) <> N'\' SET @dir += N'\';
DECLARE @log nvarchar(4000) = @dir + N'CapstoneDemo_LOG_' + FORMAT(SYSDATETIME(), 'yyyyMMdd_HHmmss') + N'.trn';

BACKUP LOG CapstoneDemo TO DISK = @log
    WITH CHECKSUM, COMPRESSION, INIT, NAME = N'CapstoneDemo LOG (capstone demo)';

SELECT @log AS log_backup_file;
GO
