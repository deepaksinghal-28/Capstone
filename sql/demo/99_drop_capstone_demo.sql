/* =============================================================================
   99_drop_capstone_demo.sql
   REMOVES the test database [CapstoneDemo] and everything in it.

   WARNING: irreversible. Only for the capstone TEST database.
   Any session using CapstoneDemo is disconnected (ROLLBACK IMMEDIATE).
   No other database is touched.

   Run in SSMS (connected to localhost) only when you deliberately want to
   rebuild the demo database with 01_create_capstone_demo.sql.
   ============================================================================= */
USE master;
GO

IF DB_ID(N'CapstoneDemo') IS NOT NULL
BEGIN
    ALTER DATABASE CapstoneDemo SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE CapstoneDemo;
    PRINT N'CapstoneDemo dropped.';
END
ELSE
    PRINT N'CapstoneDemo does not exist - nothing to do.';
GO
