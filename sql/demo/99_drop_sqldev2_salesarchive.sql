/* =============================================================================
   99_drop_sqldev2_salesarchive.sql
   REMOVES the test database [SalesArchive] from localhost\SQLDEV2.
   WARNING: irreversible. Sessions using it are disconnected (ROLLBACK IMMEDIATE).
   Refuses to run on any other instance.
   ============================================================================= */
USE master;
GO

IF @@SERVICENAME <> N'SQLDEV2'
    RAISERROR (N'Wrong instance: this script is only for localhost\SQLDEV2. Nothing was changed.', 16, 1);
ELSE IF DB_ID(N'SalesArchive') IS NOT NULL
BEGIN
    ALTER DATABASE SalesArchive SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE SalesArchive;
    PRINT N'SalesArchive dropped from SQLDEV2.';
END
ELSE
    PRINT N'SalesArchive does not exist on SQLDEV2 - nothing to do.';
GO
