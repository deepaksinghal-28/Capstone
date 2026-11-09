/* =============================================================================
   01_set_min_server_memory.sql
   Sets 'min server memory (MB)' to 512 on the LOCAL development instance.

   WHY
     On this Windows 365 Cloud PC, Windows repeatedly signals low physical memory
     (RING_BUFFER_RESOURCE_MONITOR: RESOURCE_MEMPHYSICAL_LOW, about once a minute).
     SQL Server reacts by trimming its caches, which empties the plan cache that
     Module 2 (Query Performance) reads. Once SQL Server has used 512 MB it will
     not release memory below that floor, so plan statistics survive.

   SIDE EFFECTS
     * RECONFIGURE of a memory option clears the plan cache once.
     * 'show advanced options' is switched on only for the change and switched
       back off afterwards (min server memory is an advanced option).

   UNDO
     EXEC sp_configure 'show advanced options', 1; RECONFIGURE;
     EXEC sp_configure 'min server memory (MB)', 0; RECONFIGURE;    -- previous configured value (in use: 16)
     EXEC sp_configure 'show advanced options', 0; RECONFIGURE;
   ============================================================================= */
SET NOCOUNT ON;

SELECT name, CAST(value_in_use AS int) AS value_before
FROM sys.configurations
WHERE name IN (N'min server memory (MB)', N'max server memory (MB)', N'show advanced options');

EXEC sp_configure 'show advanced options', 1;
RECONFIGURE;
EXEC sp_configure 'min server memory (MB)', 512;
RECONFIGURE;
EXEC sp_configure 'show advanced options', 0;
RECONFIGURE;

SELECT name, CAST(value_in_use AS int) AS value_after
FROM sys.configurations
WHERE name IN (N'min server memory (MB)', N'max server memory (MB)', N'show advanced options');
GO
