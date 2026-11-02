-- ============================================================
-- irs990_rebuild_indexes.sql
-- Rebuilds all disabled non-clustered indexes on the irs990
-- tables after a bulk import run.
--
-- Run AFTER the importer completes.
-- ONLINE = ON lets queries still run during the rebuild
-- (requires Azure SQL S3+ or Enterprise edition on-prem).
-- ============================================================

-- -------------------------------------------------------
-- STEP 1 — Update statistics first so the query optimizer
--           has accurate row counts immediately after rebuild
-- -------------------------------------------------------
UPDATE STATISTICS dbo.irs990_filings    WITH FULLSCAN;
UPDATE STATISTICS dbo.irs990_officers   WITH FULLSCAN;
UPDATE STATISTICS dbo.irs990_schedules  WITH FULLSCAN;
UPDATE STATISTICS dbo.irs990_pdf_cache  WITH FULLSCAN;
UPDATE STATISTICS dbo.irs990_import_log WITH FULLSCAN;
PRINT '-- Statistics updated.';

-- -------------------------------------------------------
-- STEP 2 — Rebuild all disabled non-clustered indexes
-- -------------------------------------------------------
DECLARE @sql NVARCHAR(MAX) = N'';

SELECT @sql += N'ALTER INDEX ' + QUOTENAME(i.name)
             + N' ON ' + QUOTENAME(OBJECT_SCHEMA_NAME(i.object_id))
             + N'.' + QUOTENAME(OBJECT_NAME(i.object_id))
             + N' REBUILD WITH (ONLINE = ON, SORT_IN_TEMPDB = ON);' + CHAR(13)
FROM   sys.indexes i
WHERE  OBJECT_NAME(i.object_id) LIKE 'irs990%'
  AND  i.type_desc = 'NONCLUSTERED'
  AND  i.is_disabled = 1
ORDER  BY OBJECT_NAME(i.object_id), i.name;

PRINT '-- Indexes to rebuild:';
PRINT @sql;
EXEC  sp_executesql @sql;
PRINT '-- Done. All disabled indexes rebuilt.';

-- -------------------------------------------------------
-- STEP 3 — Verify nothing left disabled
-- -------------------------------------------------------
SELECT
    OBJECT_NAME(i.object_id) AS tbl,
    i.name                   AS index_name,
    i.type_desc,
    i.is_disabled,
    ps.avg_fragmentation_in_percent AS frag_pct,
    ps.page_count
FROM  sys.indexes i
CROSS APPLY sys.dm_db_index_physical_stats(
    DB_ID(), i.object_id, i.index_id, NULL, 'LIMITED') ps
WHERE OBJECT_NAME(i.object_id) LIKE 'irs990%'
  AND i.type_desc <> 'HEAP'
ORDER BY tbl, i.type_desc DESC, i.name;
