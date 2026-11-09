/* =============================================================================
   05_create_sqldev2_salesarchive.sql
   Creates the small TEST database [SalesArchive] on the SECOND instance
   localhost\SQLDEV2, so the multi-server dashboard has two different servers.

   SAFETY
     * Refuses to run on any instance other than SQLDEV2 (@@SERVICENAME check).
     * Creates ONLY SalesArchive; stops and changes nothing if it already exists.
     * SIMPLE recovery (unlike CapstoneDemo on the primary) - shows the
       "log backup not applicable" case. ~5,000 rows of fictitious data.

   HOW TO RUN (PowerShell, project folder)
     & "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" tools\run_sql_script.py sql\demo\05_create_sqldev2_salesarchive.sql --server localhost\SQLDEV2

   UNDO
     sql\demo\99_drop_sqldev2_salesarchive.sql
   ============================================================================= */
SET NOCOUNT ON;
GO

IF @@SERVICENAME <> N'SQLDEV2'
BEGIN
    RAISERROR (N'Wrong instance: this script is only for localhost\SQLDEV2. Nothing was changed.', 16, 1);
    SET NOEXEC ON;
END
ELSE IF DB_ID(N'SalesArchive') IS NOT NULL
BEGIN
    RAISERROR (N'SalesArchive already exists on SQLDEV2 - nothing was changed.', 16, 1);
    SET NOEXEC ON;
END
GO

CREATE DATABASE SalesArchive;
GO
ALTER DATABASE SalesArchive SET RECOVERY SIMPLE;
GO

USE SalesArchive;
GO

CREATE TABLE dbo.Customers (
    CustomerID int IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Customers PRIMARY KEY CLUSTERED,
    FullName   nvarchar(100)      NOT NULL,
    Region     nvarchar(30)       NOT NULL
);

CREATE TABLE dbo.Invoices (
    InvoiceID   int IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Invoices PRIMARY KEY CLUSTERED,
    CustomerID  int                NOT NULL CONSTRAINT FK_Invoices_Customers REFERENCES dbo.Customers (CustomerID),
    InvoiceDate date               NOT NULL,
    Amount      decimal(12, 2)     NOT NULL
);
GO

WITH n AS (SELECT TOP (1000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i FROM sys.all_columns)
INSERT dbo.Customers (FullName, Region)
SELECT CONCAT(N'Archive customer ', i), CHOOSE(i % 4 + 1, N'North', N'South', N'East', N'West')
FROM n;

WITH n AS (SELECT TOP (5000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i
           FROM sys.all_columns AS a CROSS JOIN sys.all_columns AS b)
INSERT dbo.Invoices (CustomerID, InvoiceDate, Amount)
SELECT (i * 37) % 1000 + 1,
       DATEADD(DAY, -(i % 730), CAST('2026-10-31' AS date)),
       CAST(50 + (i * 13) % 950 AS decimal(12, 2))
FROM n;
GO

SELECT (SELECT COUNT(*) FROM dbo.Customers) AS Customers,
       (SELECT COUNT(*) FROM dbo.Invoices)  AS Invoices,
       DATABASEPROPERTYEX(N'SalesArchive', 'Recovery') AS RecoveryModel;
PRINT N'SalesArchive created on SQLDEV2.';
GO

SET NOEXEC OFF;
GO
