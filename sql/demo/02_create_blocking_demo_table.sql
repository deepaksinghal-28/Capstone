/* =============================================================================
   02_create_blocking_demo_table.sql
   Adds a tiny table used ONLY for the controlled blocking demonstration
   (tools\blocking_demo.py) to the TEST database [CapstoneDemo].

   SAFETY
     * Touches only CapstoneDemo. Creates the table only if it is missing.
     * 3 rows of fictitious data. The demo never commits changes to it
       (every demo transaction ends with ROLLBACK).

   HOW TO RUN
     SSMS: open, Execute (F5)      or PowerShell in the project folder:
       & "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" tools\run_sql_script.py sql\demo\02_create_blocking_demo_table.sql

   UNDO
     DROP TABLE CapstoneDemo.dbo.BlockingDemo;   (or drop the whole demo DB: 99_drop_capstone_demo.sql)
   ============================================================================= */
SET NOCOUNT ON;
GO

IF DB_ID(N'CapstoneDemo') IS NULL
BEGIN
    RAISERROR (N'CapstoneDemo does not exist - run 01_create_capstone_demo.sql first.', 16, 1);
    SET NOEXEC ON;
END
GO

USE CapstoneDemo;
GO

IF OBJECT_ID(N'dbo.BlockingDemo', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.BlockingDemo (
        Id        int          NOT NULL CONSTRAINT PK_BlockingDemo PRIMARY KEY CLUSTERED,
        Note      varchar(100) NOT NULL,
        UpdatedAt datetime2(0) NOT NULL CONSTRAINT DF_BlockingDemo_UpdatedAt DEFAULT SYSDATETIME()
    );
    INSERT dbo.BlockingDemo (Id, Note)
    VALUES (1, 'Row 1 - locked by the head blocker during the demo'),
           (2, 'Row 2 - locked by the middle session during the demo'),
           (3, 'Row 3 - never locked');
    PRINT N'dbo.BlockingDemo created with 3 rows.';
END
ELSE
    PRINT N'dbo.BlockingDemo already exists - nothing changed.';
GO

SELECT Id, Note FROM dbo.BlockingDemo ORDER BY Id;
GO

SET NOEXEC OFF;
GO
