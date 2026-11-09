/* =============================================================================
   06_backup_sqldev2.sql
   FULL backups of every database on the SECOND instance localhost\SQLDEV2
   (master, model, msdb, SalesArchive) + one LOG backup of model (FULL
   recovery), then RESTORE VERIFYONLY of each full backup file.

   Purpose: the secondary server shows "within backup policy" next to the
   primary server, which has real "no recorded full backup" problems.

   SAFETY
     * Refuses to run on any instance other than SQLDEV2.
     * Backups only READ the databases; no setting or data is changed.
     * Files go to SQLDEV2's default backup folder with time-stamped names.

   HOW TO RUN (PowerShell, project folder)
     & "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" tools\run_sql_script.py sql\demo\06_backup_sqldev2.sql --server localhost\SQLDEV2
   ============================================================================= */
SET NOCOUNT ON;
GO

IF @@SERVICENAME <> N'SQLDEV2'
BEGIN
    RAISERROR (N'Wrong instance: this script is only for localhost\SQLDEV2. Nothing was changed.', 16, 1);
    SET NOEXEC ON;
END
GO

DECLARE @dir   nvarchar(4000) = CAST(SERVERPROPERTY('InstanceDefaultBackupPath') AS nvarchar(4000));
IF RIGHT(@dir, 1) <> N'\' SET @dir += N'\';
DECLARE @stamp nvarchar(20) = FORMAT(SYSDATETIME(), 'yyyyMMdd_HHmmss');
DECLARE @db sysname, @file nvarchar(4000);
PRINT N'Backup folder: ' + @dir;

DECLARE dbs CURSOR LOCAL FAST_FORWARD FOR
    SELECT name FROM sys.databases
    WHERE name IN (N'master', N'model', N'msdb', N'SalesArchive') AND state_desc = N'ONLINE'
    ORDER BY database_id;
OPEN dbs;
FETCH NEXT FROM dbs INTO @db;
WHILE @@FETCH_STATUS = 0
BEGIN
    SET @file = @dir + @db + N'_FULL_' + @stamp + N'.bak';
    BACKUP DATABASE @db TO DISK = @file
        WITH CHECKSUM, COMPRESSION, INIT, NAME = N'SQLDEV2 full (capstone demo)';
    RESTORE VERIFYONLY FROM DISK = @file WITH CHECKSUM;
    FETCH NEXT FROM dbs INTO @db;
END
CLOSE dbs;
DEALLOCATE dbs;

-- model uses FULL recovery: one log backup so its log chain is within policy.
SET @file = @dir + N'model_LOG_' + @stamp + N'.trn';
BACKUP LOG model TO DISK = @file WITH CHECKSUM, COMPRESSION, INIT, NAME = N'SQLDEV2 model log (capstone demo)';

SELECT database_name, type, backup_finish_date
FROM msdb.dbo.backupset
WHERE backup_finish_date >= DATEADD(MINUTE, -5, SYSDATETIME())
ORDER BY backup_finish_date;
GO

SET NOEXEC OFF;
GO
