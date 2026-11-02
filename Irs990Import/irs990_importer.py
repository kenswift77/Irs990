"""
irs990_importer.py
------------------
Downloads, extracts, and imports IRS 990-series XML filings
from apps.irs.gov into SQL Server.

Usage:
    python irs990_importer.py --years 2022 2023 2024 2025 2026
    python irs990_importer.py --years 2024 --dry-run
    python irs990_importer.py --years 2025 --resume
    python irs990_importer.py --years 2023 --workers 8

Requirements:
    pip install pyodbc requests tqdm python-dotenv
    macOS: brew install p7zip   (for Deflate64 ZIPs used by IRS since ~2024)

Environment variables (or .env file):
    DB_SERVER   = your-sql-server.database.windows.net
    DB_NAME     = IRS990
    DB_USER     = youruser         (omit for Windows auth)
    DB_PASSWORD = yourpassword     (omit for Windows auth)
    DOWNLOAD_DIR = ./irs990_downloads  (optional, default shown)
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sys
import threading
import time
import shutil
import subprocess
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import pyodbc
import requests
from tqdm import tqdm

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from irs990_parser import parse_990_xml

# ---------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
                    handlers=[
                        logging.StreamHandler(sys.stdout),
                        logging.FileHandler("irs990_import.log", encoding="utf-8"),
                    ])
log = logging.getLogger(__name__)

IRS_BASE_URL    = "https://apps.irs.gov/pub/epostcard/990/xml"
REQUEST_TIMEOUT = 120       # seconds
CHUNK_SIZE      = 8 * 1024  # 8 KB read chunks while streaming
RATE_LIMIT_SEC  = 1.0       # pause between ZIP downloads (be polite to IRS servers)
MAX_RETRIES     = 3
DEFAULT_WORKERS = 4         # parallel DB writer threads per ZIP

# Known ZIP filename patterns per year.
# Scrape the IRS downloads page to get the real ZIP list rather than
# maintaining a static pattern dict that goes stale.
IRS_DOWNLOADS_PAGE = "https://www.irs.gov/charities-non-profits/form-990-series-downloads"
_ZIP_URL_RE = re.compile(
    r'https://apps\.irs\.gov/pub/epostcard/990/xml/(\d{4})/([^"\'>\s]+\.zip)',
    re.IGNORECASE,
)

def _fetch_zip_catalog() -> dict[int, list[str]]:
    """
    Scrape the IRS Form 990 downloads page and return a dict of
    {year: [zip_filename, ...]} based on what's actually listed there.
    Results are cached for the lifetime of the process.
    """
    try:
        resp = requests.get(IRS_DOWNLOADS_PAGE, timeout=30)
        resp.raise_for_status()
        catalog: dict[int, list[str]] = {}
        for m in _ZIP_URL_RE.finditer(resp.text):
            year, fname = int(m.group(1)), m.group(2)
            catalog.setdefault(year, []).append(fname)
        log.info("ZIP catalog fetched: %s",
                 {y: len(v) for y, v in sorted(catalog.items())})
        return catalog
    except Exception as exc:
        log.warning("Could not fetch ZIP catalog from IRS page: %s", exc)
        return {}

_zip_catalog: dict[int, list[str]] = {}


# ---------------------------------------------------------------
# pyodbc cursor helper
# pyodbc cursors don't implement __exit__, so we wrap them.
# ---------------------------------------------------------------

@contextmanager
def cursor_ctx(conn: pyodbc.Connection):
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


# ---------------------------------------------------------------
# Thread-local connection pool
# Each worker thread gets its own pyodbc connection.
# ---------------------------------------------------------------

_tl = threading.local()


def _is_conn_dead(exc: Exception) -> bool:
    """Return True if the exception indicates a broken/dropped TCP connection."""
    code = exc.args[0] if exc.args else ""
    msg  = str(exc)
    return code in ("08S01", "08001", "HYT00") or "TCP Provider" in msg or "Communication link failure" in msg


def _fresh_conn(existing: Optional[pyodbc.Connection] = None) -> pyodbc.Connection:
    """Close *existing* if supplied, then open and return a new connection."""
    if existing:
        try:
            existing.close()
        except Exception:
            pass
    return get_connection()


def _thread_conn() -> pyodbc.Connection:
    """Return (or lazily create) a per-thread DB connection.
    Reconnection on a dead connection is handled in _process_one_xml on error."""
    if not getattr(_tl, "conn", None):
        _tl.conn = get_connection()
    return _tl.conn


def _close_thread_conn() -> None:
    conn = getattr(_tl, "conn", None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
        _tl.conn = None


# ---------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------

def get_connection() -> pyodbc.Connection:
    server   = os.environ["DB_SERVER"]
    database = os.environ.get("DB_NAME", "IRS990")
    user     = os.environ.get("DB_USER")
    password = os.environ.get("DB_PASSWORD")

    if user and password:
        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={server};DATABASE={database};"
            f"UID={user};PWD={password};"
            f"Encrypt=yes;TrustServerCertificate=yes;"
        )
    else:
        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={server};DATABASE={database};"
            f"Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;"
        )
    return pyodbc.connect(conn_str, autocommit=False)


def upsert_filing(cursor: pyodbc.Cursor, filing: dict, log_id: int,
                  source_zip: str) -> tuple[bool, bool, Optional[int]]:
    """
    Insert a single filing row.
    Returns (inserted: bool, updated: bool, new_id: int|None).
    new_id is the IDENTITY pk returned via OUTPUT INSERTED.id — no extra SELECT needed.
    On duplicate object_id (error 2627/2601) the row is skipped and new_id is None.
    """
    params = _filing_params(filing, log_id, source_zip)
    try:
        cursor.execute(_FILING_INSERT_SQL, params)
        row = cursor.fetchone()
        new_id = int(row[0]) if row else None
        return True, False, new_id
    except pyodbc.IntegrityError as exc:
        # 2627 = unique constraint, 2601 = duplicate key index — skip silently
        if exc.args[0] in ("23000", "23"):
            return False, False, None
        msg = str(exc)
        if "2627" in msg or "2601" in msg:
            return False, False, None
        raise


def insert_officers(cursor: pyodbc.Cursor, filing_id: int, ein: str,
                    tax_year: int, officers: list[dict]) -> None:
    cursor.execute("DELETE FROM dbo.irs990_officers WHERE filing_id = ?", filing_id)
    if not officers:
        return
    sql = """
        INSERT INTO dbo.irs990_officers
            (filing_id, ein, tax_year, person_name, title,
             average_hours_per_week, reportable_comp_org, reportable_comp_related,
             other_comp, is_officer, is_director, is_trustee,
             is_key_employee, is_highest_compensated, is_former)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    rows = [
        (
            filing_id, ein, tax_year,
            o.get("person_name"), o.get("title"),
            float(o["average_hours_per_week"]) if o.get("average_hours_per_week") is not None else None,
            float(o["reportable_comp_org"])     if o.get("reportable_comp_org")     is not None else None,
            float(o["reportable_comp_related"]) if o.get("reportable_comp_related") is not None else None,
            float(o["other_comp"])              if o.get("other_comp")              is not None else None,
            1 if o.get("is_officer")             else 0,
            1 if o.get("is_director")            else 0,
            1 if o.get("is_trustee")             else 0,
            1 if o.get("is_key_employee")        else 0,
            1 if o.get("is_highest_compensated") else 0,
            1 if o.get("is_former")              else 0,
        )
        for o in officers
    ]
    cursor.executemany(sql, rows)


def insert_schedules(cursor: pyodbc.Cursor, filing_id: int, ein: str,
                     tax_year: int, schedules: list[str]) -> None:
    cursor.execute("DELETE FROM dbo.irs990_schedules WHERE filing_id = ?", filing_id)
    if not schedules:
        return
    sql = "INSERT INTO dbo.irs990_schedules (filing_id, ein, tax_year, schedule_cd) VALUES (?,?,?,?)"
    cursor.executemany(sql, [(filing_id, ein, tax_year, code) for code in schedules])


def log_zip_start(cursor: pyodbc.Cursor, year: int,
                  zip_url: str, zip_name: str, index_url: str) -> int:
    """Create or return existing import_log row. Returns -1 if already completed."""
    cursor.execute(
        "SELECT id, status FROM dbo.irs990_import_log WHERE zip_url = ?", zip_url)
    row = cursor.fetchone()
    if row:
        log_id, status = row
        if status == "completed":
            return -1
        cursor.execute(
            "UPDATE dbo.irs990_import_log SET status='downloading', started_at=SYSUTCDATETIME() WHERE id=?",
            log_id)
        return log_id
    cursor.execute(
        """INSERT INTO dbo.irs990_import_log
           (tax_year, zip_url, zip_file_name, index_csv_url, status, started_at)
           OUTPUT INSERTED.id
           VALUES (?,?,?,?,'downloading',SYSUTCDATETIME())""",
        (year, zip_url, zip_name, index_url))
    return int(cursor.fetchone()[0])


def log_zip_done(cursor: pyodbc.Cursor, log_id: int, total: int,
                 inserted: int, updated: int, failed: int) -> None:
    cursor.execute(
        """UPDATE dbo.irs990_import_log
           SET status='completed', files_in_zip=?, files_processed=?,
               files_inserted=?, files_updated=?, files_failed=?,
               completed_at=SYSUTCDATETIME()
           WHERE id=?""",
        (total, inserted + updated + failed, inserted, updated, failed, log_id))


def log_zip_error(cursor: pyodbc.Cursor, log_id: int, msg: str) -> None:
    cursor.execute(
        "UPDATE dbo.irs990_import_log SET status='failed', error_message=? WHERE id=?",
        (msg[:4000], log_id))


# ---------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------

_session = requests.Session()
_session.headers["User-Agent"] = (
    "IRS990Importer/1.0 (non-commercial research; contact: ken@netoutlook.com)"
)


def _download_stream(url: str) -> Iterator[bytes]:
    resp = _session.get(url, stream=True, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
        if chunk:
            yield chunk


def zip_exists(url: str) -> bool:
    try:
        r = _session.head(url, timeout=30)
        return r.status_code == 200
    except requests.RequestException:
        return False


# ---------------------------------------------------------------
# Per-file worker (runs inside thread pool)
# ---------------------------------------------------------------

DEADLOCK_RETRIES = 3
DEADLOCK_WAIT    = 0.5   # seconds between retries (doubles each attempt)
BATCH_SIZE       = 100   # filings per fast_executemany call


def _parse_one_xml(xml_name: str, xml_bytes: bytes, year: int) -> dict:
    """
    Parse one XML filing — no DB access.
    Safe to call from multiple threads (thread pool parse phase).
    Returns a filing dict with 'raw_xml' set.
    Raises ValueError on parse error or missing EIN.
    """
    object_id = Path(xml_name).stem

    # Strip XML declaration — SQL Server XML column rejects encoding="UTF-8"
    xml_str = xml_bytes.decode("utf-8", errors="replace")
    xml_str = re.sub(r"<\?xml[^?]*\?>", "", xml_str, count=1).strip()

    filing = parse_990_xml(xml_bytes, file_name=xml_name)
    filing["raw_xml"] = xml_str

    if "_error" in filing:
        raise ValueError(f"Parse error: {filing['_error']}")
    if not filing.get("object_id"):
        filing["object_id"] = object_id
    if not filing.get("ein"):
        raise ValueError("No EIN")
    return filing


# Shared INSERT SQL used by both upsert_filing and _batch_upsert.
# OUTPUT INSERTED.id returns the new PK in the same round-trip,
# eliminating the separate "SELECT id WHERE object_id = ?" scan.
_FILING_INSERT_SQL = """
INSERT INTO dbo.irs990_filings (
    object_id, ein, organization_name, dba_name,
    return_type, tax_year,
    tax_period_begin_date, tax_period_end_date, submission_date,
    form_version, org_address_line1, org_city, org_state, org_zip, org_country,
    exempt_status_code, group_exemption_num,
    gross_receipts, total_revenue, total_expenses, total_functional_expenses,
    net_assets_eoy, total_assets_eoy, total_liabilities_eoy,
    contributions_grants, program_service_revenue, investment_income,
    employee_count, volunteer_count,
    mission_description, program_service_desc,
    principal_officer_name, principal_officer_title,
    xml_content, xml_file_name, source_zip_file, import_log_id
) OUTPUT INSERTED.id
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _filing_params(filing: dict, log_id: int, source_zip: str) -> list:
    """Return the ordered params list for one filing INSERT row."""
    sub_date = filing.get("submission_date")
    if sub_date and "T" in sub_date:
        sub_date = sub_date[:10]
    object_id = filing.get("object_id") or filing.get("xml_file_name", "").replace(".xml", "")
    return [
        object_id,
        filing.get("ein") or "000000000",
        filing.get("organization_name") or "UNKNOWN",
        filing.get("dba_name"),
        filing.get("return_type"),
        filing.get("tax_year"),
        filing.get("tax_period_begin_date"),
        filing.get("tax_period_end_date"),
        sub_date,
        filing.get("form_version"),
        filing.get("org_address_line1"),
        filing.get("org_city"),
        filing.get("org_state"),
        filing.get("org_zip"),
        filing.get("org_country") or "US",
        filing.get("exempt_status_code"),
        filing.get("group_exemption_num"),
        float(filing["gross_receipts"])            if filing.get("gross_receipts")            is not None else None,
        float(filing["total_revenue"])             if filing.get("total_revenue")             is not None else None,
        float(filing["total_expenses"])            if filing.get("total_expenses")            is not None else None,
        float(filing["total_functional_expenses"]) if filing.get("total_functional_expenses") is not None else None,
        float(filing["net_assets_eoy"])            if filing.get("net_assets_eoy")            is not None else None,
        float(filing["total_assets_eoy"])          if filing.get("total_assets_eoy")          is not None else None,
        float(filing["total_liabilities_eoy"])     if filing.get("total_liabilities_eoy")     is not None else None,
        float(filing["contributions_grants"])      if filing.get("contributions_grants")      is not None else None,
        float(filing["program_service_revenue"])   if filing.get("program_service_revenue")   is not None else None,
        float(filing["investment_income"])         if filing.get("investment_income")         is not None else None,
        filing.get("employee_count"),
        filing.get("volunteer_count"),
        (filing.get("mission_description") or "")[:4000],
        (filing.get("program_service_desc") or "")[:4000],
        filing.get("principal_officer_name"),
        filing.get("principal_officer_title"),
        filing.get("raw_xml"),
        filing.get("xml_file_name"),
        source_zip,
        log_id,
    ]


def _batch_upsert(
    conn: pyodbc.Connection,
    filings: list[dict],
    log_id: int,
    zip_name: str,
    year: int,
) -> tuple[int, int]:
    """
    Bulk-insert a batch of parsed filings + their officers/schedules.

    Uses cursor.fast_executemany = True for the filings table (single TDS
    round-trip for up to BATCH_SIZE rows).  Falls back to row-by-row on any
    IntegrityError so duplicate-key rows are silently skipped instead of
    aborting the whole batch.

    Returns (inserted, skipped_as_duplicate).
    """
    if not filings:
        return 0, 0

    params_list = [_filing_params(f, log_id, zip_name) for f in filings]
    inserted = skipped = 0
    bulk_ok = False

    # ---- Attempt fast bulk insert ----
    try:
        with cursor_ctx(conn) as cur:
            cur.fast_executemany = True
            cur.executemany(_FILING_INSERT_SQL, params_list)
        conn.commit()
        bulk_ok = True
        inserted = len(filings)
    except pyodbc.IntegrityError:
        try:
            conn.rollback()
        except Exception:
            pass
        # At least one duplicate in the batch — fall through to row-by-row
    except pyodbc.Error as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        if _is_conn_dead(exc):
            raise
        log.warning("  Batch insert error (%s) — retrying row-by-row", exc)

    # ---- Row-by-row fallback to identify and skip individual duplicates ----
    if not bulk_ok:
        for f, params in zip(filings, params_list):
            for attempt in range(1, DEADLOCK_RETRIES + 1):
                try:
                    with cursor_ctx(conn) as cur:
                        cur.execute(_FILING_INSERT_SQL, params)
                    conn.commit()
                    inserted += 1
                    break
                except pyodbc.IntegrityError as exc:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    msg = str(exc)
                    if "2627" in msg or "2601" in msg:
                        skipped += 1
                        break
                    raise
                except pyodbc.Error as exc:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    if exc.args[0] == "40001" and attempt < DEADLOCK_RETRIES:
                        time.sleep(DEADLOCK_WAIT * (2 ** (attempt - 1)))
                        continue
                    if _is_conn_dead(exc):
                        raise
                    log.error("  Row insert error for %s: %s", f.get("object_id"), exc)
                    skipped += 1
                    break

    # ---- Officers + schedules for filings that landed in the DB ----
    # Batch all rows for the entire flush into two executemany calls
    # (one for officers, one for schedules) instead of N*officers round-trips.
    if inserted > 0:
        object_ids = [f.get("object_id") for f in filings]
        placeholders = ",".join("?" * len(object_ids))
        with cursor_ctx(conn) as cur:
            cur.execute(
                f"SELECT id, object_id FROM dbo.irs990_filings "
                f"WHERE object_id IN ({placeholders})",
                object_ids,
            )
            id_map: dict[str, int] = {row[1]: row[0] for row in cur.fetchall()}

        _OFFICER_SQL = """
            INSERT INTO dbo.irs990_officers
                (filing_id, ein, tax_year, person_name, title,
                 average_hours_per_week, reportable_comp_org, reportable_comp_related,
                 other_comp, is_officer, is_director, is_trustee,
                 is_key_employee, is_highest_compensated, is_former)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        _SCHEDULE_SQL = (
            "INSERT INTO dbo.irs990_schedules "
            "(filing_id, ein, tax_year, schedule_cd) VALUES (?,?,?,?)"
        )

        all_officers: list[tuple] = []
        all_schedules: list[tuple] = []
        for f in filings:
            db_id = id_map.get(f.get("object_id"))
            if db_id is None:
                continue
            ein      = f["ein"]
            tax_year = f.get("tax_year") or year
            for o in f.get("officers", []):
                all_officers.append((
                    db_id, ein, tax_year,
                    o.get("person_name"), o.get("title"),
                    float(o["average_hours_per_week"]) if o.get("average_hours_per_week") is not None else None,
                    float(o["reportable_comp_org"])     if o.get("reportable_comp_org")     is not None else None,
                    float(o["reportable_comp_related"]) if o.get("reportable_comp_related") is not None else None,
                    float(o["other_comp"])              if o.get("other_comp")              is not None else None,
                    1 if o.get("is_officer")             else 0,
                    1 if o.get("is_director")            else 0,
                    1 if o.get("is_trustee")             else 0,
                    1 if o.get("is_key_employee")        else 0,
                    1 if o.get("is_highest_compensated") else 0,
                    1 if o.get("is_former")              else 0,
                ))
            for code in f.get("schedules", []):
                all_schedules.append((db_id, ein, tax_year, code))

        with cursor_ctx(conn) as cur:
            cur.fast_executemany = True
            if all_officers:
                cur.executemany(_OFFICER_SQL, all_officers)
            if all_schedules:
                cur.executemany(_SCHEDULE_SQL, all_schedules)
        conn.commit()

    return inserted, skipped


def _process_one_xml(
    xml_name: str,
    xml_bytes_filing: bytes,
    year: int,
    log_id: int,
    zip_name: str,
) -> tuple[bool, bool]:
    """
    Parse one XML filing and upsert it into the DB.
    Uses a thread-local connection — safe to call from multiple threads.
    Automatically retries on SQL Server deadlock (error 40001).
    Returns (inserted, updated).  Raises on unrecoverable error.
    """
    object_id = Path(xml_name).stem

    # Strip XML declaration — SQL Server XML column rejects encoding="UTF-8"
    xml_str = xml_bytes_filing.decode("utf-8", errors="replace")
    xml_str = re.sub(r"<\?xml[^?]*\?>", "", xml_str, count=1).strip()

    filing = parse_990_xml(xml_bytes_filing, file_name=xml_name)
    filing["raw_xml"] = xml_str

    if "_error" in filing:
        raise ValueError(f"Parse error: {filing['_error']}")

    if not filing.get("object_id"):
        filing["object_id"] = object_id

    if not filing.get("ein"):
        raise ValueError("No EIN")

    for attempt in range(1, DEADLOCK_RETRIES + 1):
        conn = _thread_conn()
        try:
            with cursor_ctx(conn) as cur:
                # OUTPUT INSERTED.id returns the pk inline — no extra SELECT.
                ins, upd, filing_db_id = upsert_filing(cur, filing, log_id, zip_name)
                if filing_db_id is not None:
                    # New row — insert related records.
                    # Duplicate rows already have their officers/schedules from
                    # the original import; skip them to avoid re-doing the work.
                    insert_officers(cur, filing_db_id, filing["ein"],
                                    filing.get("tax_year") or year,
                                    filing.get("officers", []))
                    insert_schedules(cur, filing_db_id, filing["ein"],
                                     filing.get("tax_year") or year,
                                     filing.get("schedules", []))
            conn.commit()
            return ins, upd

        except pyodbc.Error as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            # 40001 = deadlock victim — retry after a short back-off
            if exc.args[0] == "40001" and attempt < DEADLOCK_RETRIES:
                wait = DEADLOCK_WAIT * (2 ** (attempt - 1))
                log.debug("  Deadlock on %s (attempt %d) — retrying in %.1fs",
                          xml_name, attempt, wait)
                time.sleep(wait)
                continue
            # 08S01 / TCP drop — reconnect and retry once
            if _is_conn_dead(exc) and attempt < DEADLOCK_RETRIES:
                log.warning("  Thread DB connection lost — reconnecting (%s)", xml_name)
                _close_thread_conn()
                time.sleep(1)
                continue
            _close_thread_conn()
            raise
        except Exception:
            conn.rollback()
            _close_thread_conn()
            raise

    raise RuntimeError(f"Deadlock unresolved after {DEADLOCK_RETRIES} attempts: {xml_name}")


# ---------------------------------------------------------------
# Core ZIP processor
# ---------------------------------------------------------------

def _load_existing_object_ids(conn: pyodbc.Connection, zip_name: str) -> set[str]:
    """
    Return the set of object_ids already in the DB for this ZIP.
    Used by incremental mode to skip files we've already processed.
    """
    with cursor_ctx(conn) as cur:
        cur.execute(
            "SELECT object_id FROM dbo.irs990_filings WHERE source_zip_file = ?",
            zip_name)
        return {row[0] for row in cur.fetchall()}


def _extract_xml_items(zip_bytes: bytes, names: list[str]) -> list[tuple[str, bytes]]:
    """
    Extract named entries from zip_bytes as (name, bytes) pairs.

    Tries Python's zipfile first (fast, no dependencies).
    If the ZIP uses Deflate64 (method 9), falls back to the system
    `7z` command (install via: brew install p7zip).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            return [(n, zf.read(n)) for n in names]
    except NotImplementedError:
        pass  # Deflate64 — fall through to 7z

    # 7z fallback: write ZIP to a temp file, extract, read back
    sevenzip = shutil.which("7z") or shutil.which("7za")
    if not sevenzip:
        raise RuntimeError(
            "ZIP uses Deflate64 compression but '7z' was not found. "
            "Install it with: brew install p7zip"
        )

    log.info("  ZIP uses Deflate64 — extracting via 7z ...")
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "archive.zip"
        zip_path.write_bytes(zip_bytes)
        subprocess.run(
            [sevenzip, "x", str(zip_path), "-o" + tmp, "-y"],
            check=True,
            capture_output=True,
        )
        result = []
        for name in names:
            # 7z preserves internal path structure
            extracted = Path(tmp) / name
            if not extracted.exists():
                # Some ZIPs have a flat layout despite internal paths
                extracted = Path(tmp) / Path(name).name
            result.append((name, extracted.read_bytes()))
    return result


def process_zip(zip_url: str, year: int,
                download_dir: Path, dry_run: bool, workers: int = DEFAULT_WORKERS,
                incremental: bool = False) -> None:
    zip_name  = zip_url.rsplit("/", 1)[-1]
    index_url = f"{IRS_BASE_URL}/{year}/index_{year}.csv"
    local_zip = download_dir / zip_name

    log.info("=== Processing %s ===", zip_name)

    # Fresh main-thread connection per ZIP — avoids 08S01 on long imports
    conn: Optional[pyodbc.Connection] = None if dry_run else get_connection()

    # -- Idempotency check (skipped in dry-run) --
    log_id = 0
    if not dry_run and conn is not None:
        with cursor_ctx(conn) as cur:
            log_id = log_zip_start(cur, year, zip_url, zip_name, index_url)
            conn.commit()
        if log_id == -1:
            log.info("  Already completed — skipping.")
            conn.close()
            return

    # -- Download --
    if not local_zip.exists():
        log.info("  Downloading %s ...", zip_url)
        buf = io.BytesIO()
        for chunk in _download_stream(zip_url):
            buf.write(chunk)
        if not dry_run:
            local_zip.write_bytes(buf.getvalue())
        zip_bytes = buf.getvalue()
    else:
        log.info("  Using cached %s", local_zip)
        zip_bytes = local_zip.read_bytes()

    if dry_run:
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                xml_count = sum(1 for n in zf.namelist() if n.lower().endswith(".xml"))
            log.info("  [DRY RUN] %d bytes downloaded, %d XML files inside",
                     len(zip_bytes), xml_count)
        except zipfile.BadZipFile:
            log.warning("  [DRY RUN] File is not a valid ZIP")
        return

    # -- Extract all XML bytes up front (ZIP reads are not thread-safe) --
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            total_in_zip = len(xml_names)

            # Incremental mode: skip files whose object_id is already in the DB
            if incremental and conn is not None:
                log.info("  %d XML files in ZIP — checking against DB for new files ...",
                         total_in_zip)
                existing_ids = _load_existing_object_ids(conn, zip_name)
                xml_names = [n for n in xml_names
                             if Path(n).stem not in existing_ids]
                skipped = total_in_zip - len(xml_names)
                log.info("  %d already loaded, %d new to process",
                         skipped, len(xml_names))
            else:
                log.info("  %d XML files — reading into memory with %d workers ...",
                         total_in_zip, workers)

            total = len(xml_names)

        # Read all at once outside the ZipFile context (not thread-safe).
        # Falls back to 7z for Deflate64 ZIPs (IRS started using this ~2024).
        xml_items: list[tuple[str, bytes]] = _extract_xml_items(zip_bytes, xml_names)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        with cursor_ctx(conn) as cur:
            log_zip_error(cur, log_id, str(exc))
            conn.commit()
        log.error("  Failed to extract ZIP %s: %s", zip_name, exc)
        return

    # Nothing new to do in incremental mode
    if total == 0:
        log.info("  No new files to process — ZIP is fully up to date.")
        if conn:
            conn.close()
        return

    # -- Parallel parse + insert --
    # Each thread does parse → INSERT filing → INSERT officers/schedules.
    # insert_officers/insert_schedules now use executemany internally,
    # so each thread makes only 3 DB round-trips per filing instead of N+2.
    inserted = updated = failed = 0
    lock = threading.Lock()

    def _submit(item: tuple[str, bytes]) -> tuple[bool, bool]:
        name, data = item
        return _process_one_xml(name, data, year, log_id, zip_name)

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="irs990") as pool:
        futures = {pool.submit(_submit, item): item[0] for item in xml_items}
        with tqdm(total=total, desc=zip_name, unit="file", leave=False) as bar:
            for future in as_completed(futures):
                xml_name = futures[future]
                try:
                    ins, upd = future.result()
                    with lock:
                        if ins:
                            inserted += 1
                        elif upd:
                            updated += 1
                except Exception as exc:
                    log.error("  Error processing %s: %s", xml_name, exc)
                    with lock:
                        failed += 1
                finally:
                    bar.update(1)

    # -- Finalise log row using the main connection --
    # Reconnect once if Azure SQL dropped the idle connection during the import.
    for _attempt in range(2):
        try:
            with cursor_ctx(conn) as cur:
                log_zip_done(cur, log_id, total, inserted, updated, failed)
                conn.commit()
            break
        except Exception as exc:
            if _attempt == 0 and _is_conn_dead(exc):
                log.warning("Main DB connection lost — reconnecting for log update")
                conn = _fresh_conn(conn)
            else:
                log.error("  Failed to update import log: %s", exc)
                break

    log.info("  Done: %d inserted, %d updated, %d failed (workers=%d)",
             inserted, updated, failed, workers)

    if conn:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------

def get_zip_urls_for_year(year: int) -> list[str]:
    global _zip_catalog
    if not _zip_catalog:
        _zip_catalog = _fetch_zip_catalog()
    candidates = _zip_catalog.get(year, [])
    urls = [f"{IRS_BASE_URL}/{year}/{name}" for name in candidates]
    if not urls:
        log.warning("No ZIPs found for year %d — check IRS downloads page", year)
    return urls


# ---------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import IRS 990 XML filings into SQL Server")
    parser.add_argument("--years", nargs="+", type=int,
                        default=[2022, 2023, 2024, 2025, 2026],
                        help="Tax years to download and import")
    parser.add_argument("--dry-run", action="store_true",
                        help="Download and count files but don't write to DB")
    parser.add_argument("--resume", action="store_true",
                        help="Skip ZIPs already marked 'completed' in DB")
    parser.add_argument("--download-dir", default="./irs990_downloads",
                        help="Directory to cache downloaded ZIPs")
    parser.add_argument("--rate-limit", type=float, default=RATE_LIMIT_SEC,
                        help="Seconds to wait between ZIP downloads")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="Parallel DB writer threads per ZIP (default 4)")
    parser.add_argument("--year-zip", nargs=2, metavar=("YEAR", "ZIP_URL"),
                        help="Process a single specific ZIP URL")
    parser.add_argument("--incremental", action="store_true",
                        help="Skip files whose object_id is already in the DB "
                             "(fast sync for in-progress years like 2026)")
    args = parser.parse_args()

    dl_dir = Path(args.download_dir)
    dl_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        log.info("DRY RUN mode — no data will be written to DB")

    if args.incremental:
        log.info("INCREMENTAL mode — only new object_ids will be processed")

    try:
        if args.year_zip:
            year, zip_url = int(args.year_zip[0]), args.year_zip[1]
            process_zip(zip_url, year, dl_dir, args.dry_run, args.workers,
                        incremental=args.incremental)
        else:
            for year in sorted(args.years):
                log.info(">>> Year %d <<<", year)
                zip_urls = get_zip_urls_for_year(year)
                log.info("  Found %d ZIP files for %d", len(zip_urls), year)
                for i, url in enumerate(zip_urls):
                    process_zip(url, year, dl_dir, args.dry_run, args.workers,
                                incremental=args.incremental)
                    if i < len(zip_urls) - 1:
                        time.sleep(args.rate_limit)
    except KeyboardInterrupt:
        log.info("Interrupted.")

    log.info("Import complete.")


if __name__ == "__main__":
    main()
