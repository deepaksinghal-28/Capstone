/* =============================================================================
   03_backup_capstone_demo.sql
   Takes REAL backups of the TEST database [CapstoneDemo] for the backup
   monitoring demonstration:  FULL  ->  DIFFERENTIAL  ->  LOG
   and then checks the full backup file with RESTORE VERIFYONLY (no restore).

   SAFETY / SIDE EFFECTS
     * Only CapstoneDemo is backed up. No data is changed.
     * Files go to SQL Server's default backup folder
       (SERVERPROPERTY('InstanceDefaultBackupPath')), with time-stamped names,
       so nothing is overwritten. Compressed: roughly 5-15 MB in total.
     * msdb records the backup history (that is what Module 4 reads).
     * CapstoneDemo uses FULL recovery: after this first full backup the log
       chain starts, so the log is only reused after LOG backups
       (run 04_backup_capstone_log.sql, or switch the test DB to SIMPLE).

   HOW TO RUN
     SSMS: open, Execute (F5)      or PowerShell in the project folder:
       & "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" tools\run_sql_script.py sql\demo\03_backup_capstone_demo.sql
   ============================================================================= */
SET NOCOUNT ON;
GO

IF DB_ID(N'CapstoneDemo') IS NULL
BEGIN
    RAISERROR (N'CapstoneDemo does not exist - run 01_create_capstone_demo.sql first.', 16, 1);
    SET NOEXEC ON;
END
GO

DECLARE @dir   nvarchar(4000) = CAST(SERVERPROPERTY('InstanceDefaultBackupPath') AS nvarchar(4000));
IF RIGHT(@dir, 1) <> N'\' SET @dir += N'\';
DECLARE @stamp nvarchar(20)   = FORMAT(SYSDATETIME(), 'yyyyMMdd_HHmmss');
DECLARE @full  nvarchar(4000) = @dir + N'CapstoneDemo_FULL_' + @stamp + N'.bak';
DECLARE @diff  nvarchar(4000) = @dir + N'CapstoneDemo_DIFF_' + @stamp + N'.bak';
DECLARE @log   nvarchar(4000) = @dir + N'CapstoneDemo_LOG_'  + @stamp + N'.trn';

PRINT N'Backup folder: ' + @dir;

-- 1. Full backup (starts the backup chain)
BACKUP DATABASE CapstoneDemo TO DISK = @full
    WITH CHECKSUM, COMPRESSION, INIT, NAME = N'CapstoneDemo FULL (capstone demo)';

-- 2. Differential backup (changes since the full)
BACKUP DATABASE CapstoneDemo TO DISK = @diff
    WITH DIFFERENTIAL, CHECKSUM, COMPRESSION, INIT, NAME = N'CapstoneDemo DIFF (capstone demo)';

-- 3. Transaction-log backup (FULL recovery model)
BACKUP LOG CapstoneDemo TO DISK = @log
    WITH CHECKSUM, COMPRESSION, INIT, NAME = N'CapstoneDemo LOG (capstone demo)';

-- 4. Check that the full backup file is complete and readable (does NOT restore anything)
RESTORE VERIFYONLY FROM DISK = @full WITH CHECKSUM;

SELECT @full AS full_backup_file, @diff AS diff_backup_file, @log AS log_backup_file;
GO

SET NOEXEC OFF;
GO
