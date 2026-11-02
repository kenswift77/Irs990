-- ============================================================
-- irs990_truncate.sql
-- Clears all imported data so you can re-run the importer
-- from scratch.  Run in this exact order (child tables first)
-- to satisfy foreign key constraints.
-- ============================================================

-- 1. Child tables first
TRUNCATE TABLE dbo.irs990_schedules;
TRUNCATE TABLE dbo.irs990_officers;
TRUNCATE TABLE dbo.irs990_pdf_cache;

-- 2. Main filings table
TRUNCATE TABLE dbo.irs990_filings;

-- 3. Import log (reset so all ZIPs will re-process)
TRUNCATE TABLE dbo.irs990_import_log;

-- Verify row counts after truncate
SELECT 'irs990_filings'    AS tbl, COUNT(*) AS rows FROM dbo.irs990_filings
UNION ALL
SELECT 'irs990_officers',          COUNT(*)         FROM dbo.irs990_officers
UNION ALL
SELECT 'irs990_schedules',         COUNT(*)         FROM dbo.irs990_schedules
UNION ALL
SELECT 'irs990_pdf_cache',         COUNT(*)         FROM dbo.irs990_pdf_cache
UNION ALL
SELECT 'irs990_import_log',        COUNT(*)         FROM dbo.irs990_import_log;
