/* =============================================================================
   01_create_capstone_demo.sql
   Creates the TEST database [CapstoneDemo] used for the capstone demonstrations.

   SAFETY
     * Creates ONLY the database CapstoneDemo and objects inside it.
     * Does NOT touch master/model/msdb/tempdb or any other database.
     * If CapstoneDemo already exists, the script stops and changes nothing
       (SET NOEXEC ON skips every remaining batch, also in SSMS).
     * All data is generated, fictitious test data.

   CONTENTS
     dbo.Customers   20,000 rows   (no index on Email   -> LIKE '%x%' scans)
     dbo.Products       200 rows
     dbo.Orders     200,000 rows   (no index on Status  -> full scans)
     5 stored procedures with deliberately different costs (cheap seek ... heavy scan)

   HOW TO RUN
     SSMS: connect to localhost, open this file, press Execute (F5).
     or PowerShell in the project folder:
       & "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" tools\run_sql_script.py sql\demo\01_create_capstone_demo.sql

   UNDO
     sql\demo\99_drop_capstone_demo.sql
   ============================================================================= */
SET NOCOUNT ON;
GO

IF DB_ID(N'CapstoneDemo') IS NOT NULL
BEGIN
    RAISERROR (N'CapstoneDemo already exists - nothing was changed. Run 99_drop_capstone_demo.sql first if you want to rebuild it.', 16, 1);
    SET NOEXEC ON;      -- skip every batch below
END
GO

-- -----------------------------------------------------------------------------
-- 1. Database (default file locations; FULL recovery for the backup module)
-- -----------------------------------------------------------------------------
CREATE DATABASE CapstoneDemo;
GO
ALTER DATABASE CapstoneDemo SET RECOVERY FULL;
ALTER DATABASE CapstoneDemo MODIFY FILE (NAME = N'CapstoneDemo',     SIZE = 64MB, FILEGROWTH = 64MB);
ALTER DATABASE CapstoneDemo MODIFY FILE (NAME = N'CapstoneDemo_log', SIZE = 64MB, FILEGROWTH = 64MB);
GO

USE CapstoneDemo;
GO

-- -----------------------------------------------------------------------------
-- 2. Tables
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.Customers (
    CustomerID  int IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Customers PRIMARY KEY CLUSTERED,
    FullName    nvarchar(100)      NOT NULL,
    City        nvarchar(50)       NOT NULL,
    Email       nvarchar(200)      NOT NULL,     -- intentionally NOT indexed
    CreatedAt   datetime2(0)       NOT NULL
);

CREATE TABLE dbo.Products (
    ProductID   int IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Products PRIMARY KEY CLUSTERED,
    ProductName nvarchar(100)      NOT NULL,
    Category    nvarchar(50)       NOT NULL,
    UnitPrice   decimal(10, 2)     NOT NULL
);

CREATE TABLE dbo.Orders (
    OrderID     int IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Orders PRIMARY KEY CLUSTERED,
    CustomerID  int                NOT NULL CONSTRAINT FK_Orders_Customers REFERENCES dbo.Customers (CustomerID),
    ProductID   int                NOT NULL CONSTRAINT FK_Orders_Products  REFERENCES dbo.Products (ProductID),
    Quantity    int                NOT NULL,
    Amount      decimal(12, 2)     NOT NULL,
    OrderDate   datetime2(0)       NOT NULL,
    Status      varchar(20)        NOT NULL      -- intentionally NOT indexed
);
GO

-- -----------------------------------------------------------------------------
-- 3. Generated test data (deterministic, set-based)
-- -----------------------------------------------------------------------------
WITH n AS (
    SELECT TOP (20000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i
    FROM sys.all_columns AS a CROSS JOIN sys.all_columns AS b
)
INSERT dbo.Customers (FullName, City, Email, CreatedAt)
SELECT CONCAT(N'Customer ', i),
       CHOOSE(i % 8 + 1, N'Mumbai', N'Delhi', N'Bengaluru', N'Chennai',
                         N'Kolkata', N'Pune', N'Hyderabad', N'Jaipur'),
       CONCAT(N'customer', i, N'@example.com'),
       DATEADD(DAY, -(i % 1000), CAST('2026-01-01' AS datetime2(0)))
FROM n;

WITH n AS (
    SELECT TOP (200) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i
    FROM sys.all_columns
)
INSERT dbo.Products (ProductName, Category, UnitPrice)
SELECT CONCAT(N'Product ', i),
       CHOOSE(i % 5 + 1, N'Electronics', N'Books', N'Clothing', N'Home', N'Sports'),
       CAST(100 + (i * 37) % 4900 AS decimal(10, 2))
FROM n;

WITH n AS (
    SELECT TOP (200000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i
    FROM sys.all_columns AS a CROSS JOIN sys.all_columns AS b
)
INSERT dbo.Orders (CustomerID, ProductID, Quantity, Amount, OrderDate, Status)
SELECT (n.i * 7919) % 20000 + 1,
       p.ProductID,
       n.i % 5 + 1,
       (n.i % 5 + 1) * p.UnitPrice,
       DATEADD(MINUTE, -((n.i * 13) % 525600), CAST('2026-11-01' AS datetime2(0))),
       CHOOSE(n.i % 4 + 1, 'New', 'Shipped', 'Delivered', 'Cancelled')
FROM n
JOIN dbo.Products AS p ON p.ProductID = (n.i * 31) % 200 + 1;
GO

-- Supporting index for the "cheap" customer lookup only.
CREATE NONCLUSTERED INDEX IX_Orders_CustomerID
    ON dbo.Orders (CustomerID) INCLUDE (OrderDate, Amount, Status);
GO

-- -----------------------------------------------------------------------------
-- 4. Stored procedures with deliberately different performance profiles
-- -----------------------------------------------------------------------------

-- Cheap: index seek, called very often.
CREATE PROCEDURE dbo.usp_GetCustomerOrders @CustomerID int
AS
BEGIN
    SET NOCOUNT ON;
    SELECT o.OrderID, o.OrderDate, o.Amount, o.Status
    FROM dbo.Orders AS o
    WHERE o.CustomerID = @CustomerID
    ORDER BY o.OrderDate DESC;
END;
GO

-- Medium: join + aggregate over a date range.
CREATE PROCEDURE dbo.usp_SalesByCity @FromDate datetime2(0)
AS
BEGIN
    SET NOCOUNT ON;
    SELECT c.City, COUNT(*) AS Orders, SUM(o.Amount) AS Revenue
    FROM dbo.Orders AS o
    JOIN dbo.Customers AS c ON c.CustomerID = o.CustomerID
    WHERE o.OrderDate >= @FromDate
    GROUP BY c.City
    ORDER BY Revenue DESC;
END;
GO

-- Read-heavy: leading-wildcard LIKE cannot use an index -> scans Customers.
CREATE PROCEDURE dbo.usp_FindCustomersByEmail @Fragment nvarchar(50)
AS
BEGIN
    SET NOCOUNT ON;
    SELECT TOP (50) c.CustomerID, c.FullName, c.Email
    FROM dbo.Customers AS c
    WHERE c.Email LIKE N'%' + @Fragment + N'%';
END;
GO

-- Read-heavy: no index on Status -> scans all 200,000 orders every call.
CREATE PROCEDURE dbo.usp_OrdersByStatus @Status varchar(20)
AS
BEGIN
    SET NOCOUNT ON;
    SELECT COUNT(*) AS Orders, SUM(o.Amount) AS Amount, MAX(o.OrderDate) AS LatestOrder
    FROM dbo.Orders AS o
    WHERE o.Status = @Status;
END;
GO

-- CPU-heavy: aggregate + window functions over every order.
CREATE PROCEDURE dbo.usp_ProductRevenueReport
AS
BEGIN
    SET NOCOUNT ON;
    WITH monthly AS (
        SELECT p.Category, p.ProductName,
               DATEFROMPARTS(YEAR(o.OrderDate), MONTH(o.OrderDate), 1) AS OrderMonth,
               SUM(o.Amount) AS Revenue
        FROM dbo.Orders AS o
        JOIN dbo.Products AS p ON p.ProductID = o.ProductID
        GROUP BY p.Category, p.ProductName, DATEFROMPARTS(YEAR(o.OrderDate), MONTH(o.OrderDate), 1)
    )
    SELECT TOP (20) Category, ProductName, OrderMonth, Revenue,
           SUM(Revenue) OVER (PARTITION BY ProductName ORDER BY OrderMonth
                              ROWS UNBOUNDED PRECEDING) AS RunningRevenue,
           RANK() OVER (PARTITION BY OrderMonth ORDER BY Revenue DESC) AS MonthRank
    FROM monthly
    ORDER BY Revenue DESC;
END;
GO

-- -----------------------------------------------------------------------------
-- 5. Summary
-- -----------------------------------------------------------------------------
SELECT (SELECT COUNT(*) FROM dbo.Customers) AS Customers,
       (SELECT COUNT(*) FROM dbo.Products)  AS Products,
       (SELECT COUNT(*) FROM dbo.Orders)    AS Orders;
PRINT N'CapstoneDemo created successfully.';
GO

SET NOEXEC OFF;
GO
