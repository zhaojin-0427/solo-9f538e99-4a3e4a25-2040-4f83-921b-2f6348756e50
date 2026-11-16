"""Numeric-tag search: configuration validation, index rebuilds and queries.

Build/switch lifecycle
----------------------
* A submitted configuration is stored as a ``search_config`` row in status
  ``building``; the previously active configuration keeps serving searches.
* A single process-wide background worker rebuilds the index for the newest
  pending version by scanning every *published* product.  Uploads keep working
  while the scan runs: they index themselves against the currently active
  version inside their own batch transaction, and the rebuild performs a final
  coverage pass (holding the batch lock) so nothing published before the flip
  is missing from the new index.
* Switching (``building`` -> ``active``, previous active -> ``superseded``)
  happens in one SQLite transaction together with the coverage check, so the
  active version and its index always switch atomically.
* A build superseded by a newer configuration is cancelled and can never
  activate; failed/interrupted builds stay on the old version and are retried
  automatically after a restart (or manually via the retry action).
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import traceback
from datetime import datetime, timezone

import pydicom
import pydicom.datadict

# Numeric value representations accepted as configuration targets.  Binary
# array VRs (OW/OB/OF/OD/OL/UN), strings (SH/LO ...) and sequences are out.
NUMERIC_VRS = frozenset({"DS", "IS", "FL", "FD", "SS", "US", "SL", "UL"})
MAX_FIELDS = 3
# Products are indexed in chunks so uploads can take the write lock in between.
CHUNK_SIZE = 100
# How often the idle worker looks for work (also woken immediately on submit).
IDLE_SCAN_SECONDS = 2.0

REASON_LABELS = {
    "missing": "标签缺失",
    "empty": "值为空",
    "non_numeric": "非数值",
    "not_finite": "非有限数值（NaN/无穷大）",
    "multi_value": "非单个数值（多值）",
    "read_error": "成品读取失败",
}


class SearchConfigError(ValueError):
    """Invalid search configuration submitted from the browser."""


# ---------------------------------------------------------------------------
# Configuration validation and numeric extraction
# ---------------------------------------------------------------------------

def validate_fields(keywords: list[str]) -> list[str]:
    """Normalise and validate 1..3 top-level standard numeric tag keywords."""
    cleaned = [(kw or "").strip() for kw in keywords]
    cleaned = [kw for kw in cleaned if kw]
    if not cleaned:
        raise SearchConfigError("至少配置 1 个检索字段。")
    if len(cleaned) > MAX_FIELDS:
        raise SearchConfigError(f"最多配置 {MAX_FIELDS} 个检索字段。")
    if len(set(cleaned)) != len(cleaned):
        raise SearchConfigError("检索字段不能重复。")

    validated: list[str] = []
    # Case-insensitive keyword lookup table built from the standard dictionary.
    keyword_index = {kw.lower(): kw for kw in pydicom.datadict.keyword_dict}
    for keyword in cleaned:
        canonical_input = keyword_index.get(keyword.lower())
        if canonical_input is None:
            raise SearchConfigError(f"字段 {keyword} 不是 pydicom 字典中的标准 DICOM 标签。")
        tag = pydicom.datadict.tag_for_keyword(canonical_input)
        # Top-level standard data tags only: no file meta (0002), no command
        # set (0000), no private tags (odd group) and no group-length elements.
        group = int(tag) >> 16
        if group < 0x0008 or group % 2 == 1 or (int(tag) & 0xFFFF) == 0:
            raise SearchConfigError(f"字段 {keyword} 不是可用的顶层标准数据标签。")
        try:
            vr = pydicom.datadict.dictionary_VR(tag)
        except KeyError as exc:
            raise SearchConfigError(f"字段 {keyword} 缺少 VR 定义。") from exc
        if vr not in NUMERIC_VRS:
            raise SearchConfigError(
                f"字段 {keyword} 的 VR 为 {vr}，仅支持标准数值型标签"
                f"（{', '.join(sorted(NUMERIC_VRS))}）。"
            )
        # A field that is defined as multi-valued (VM 2/3/1-n ...) can never
        # hold "a single finite number", so it would only ever produce misses.
        vm = pydicom.datadict.dictionary_VM(tag)
        if vm != "1":
            raise SearchConfigError(
                f"字段 {keyword} 的值多重性为 {vm}，检索字段必须是单值标签（VM=1）。"
            )
        canonical = pydicom.datadict.keyword_for_tag(tag)
        if canonical in validated:
            raise SearchConfigError(f"字段 {keyword} 与其他字段指向同一标签（重复）。")
        validated.append(canonical)
    return validated


def extract_numeric(ds: pydicom.Dataset, keyword: str) -> tuple[float | None, str | None]:
    """Return ``(value, reason)`` for one configured top-level numeric tag.

    A hit requires the tag to be present, hold exactly one value that parses
    as a finite number.  Otherwise the failure reason is one of the codes
    stored in ``search_exclusion.reason``.
    """
    tag = pydicom.datadict.tag_for_keyword(keyword)
    if not tag or tag not in ds:  # Dataset.__contains__ is top-level only
        return None, "missing"
    element = ds[tag]
    value = element.value
    if value is None:
        return None, "empty"

    # pydicom MultiValue is a MutableSequence, never a str/bytes.
    if isinstance(value, (list, tuple)) or type(value).__name__ == "MultiValue":
        values = list(value)
        if not values:
            return None, "empty"
        if len(values) != 1:
            return None, "multi_value"
        scalar = values[0]
    else:
        scalar = value

    if isinstance(scalar, bytes):
        return None, "non_numeric"
    if isinstance(scalar, str) and scalar == "":
        return None, "empty"
    try:
        number = float(scalar)
    except (TypeError, ValueError):
        return None, "non_numeric"
    if not math.isfinite(number):
        return None, "not_finite"
    return number, None


def parse_fields(fields_json: str) -> list[str]:
    return json.loads(fields_json)


# ---------------------------------------------------------------------------
# Indexing primitives
# ---------------------------------------------------------------------------

def _index_one(conn: sqlite3.Connection, config_id: int, fields: list[str],
               hospital_id: int, batch_file_id: int, sop_uid_old: str,
               ds: pydicom.Dataset) -> str:
    """Index one product against one config version.

    Returns ``"indexed"`` or ``"excluded"``.  The first failing field decides
    the exclusion reason; unique constraints make re-runs idempotent.
    """
    values: list[float] = []
    for keyword in fields:
        number, reason = extract_numeric(ds, keyword)
        if reason is not None:
            conn.execute(
                "INSERT OR IGNORE INTO search_exclusion(config_id, hospital_id, "
                "batch_file_id, sop_uid_old, field_keyword, reason, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (config_id, hospital_id, batch_file_id, sop_uid_old,
                 keyword, reason, REASON_LABELS[reason]),
            )
            return "excluded"
        values.append(number)

    padded = values + [None] * (MAX_FIELDS - len(values))
    conn.execute(
        "INSERT OR IGNORE INTO search_index(config_id, hospital_id, batch_file_id, "
        "sop_uid_old, value_1, value_2, value_3) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (config_id, hospital_id, batch_file_id, sop_uid_old, *padded),
    )
    return "indexed"


def index_dataset_for_active(conn: sqlite3.Connection, hospital_id: int,
                             batch_file_id: int, sop_uid_old: str,
                             ds: pydicom.Dataset) -> bool:
    """Incrementally index a freshly published product against the active config.

    Runs inside the batch publication transaction, so an upload failure rolls
    the index rows back together with everything else.  Returns whether an
    active configuration existed.
    """
    active = conn.execute(
        "SELECT id, fields_json FROM search_config WHERE status = 'active'"
    ).fetchone()
    if active is None:
        return False
    fields = parse_fields(active["fields_json"])
    outcome = _index_one(conn, active["id"], fields, hospital_id, batch_file_id,
                         sop_uid_old, ds)
    if outcome == "indexed":
        conn.execute(
            "UPDATE search_config SET indexed_products = indexed_products + 1, "
            "total_products = total_products + 1 WHERE id = ?",
            (active["id"],),
        )
    else:
        conn.execute(
            "UPDATE search_config SET excluded_count = excluded_count + 1, "
            "total_products = total_products + 1 WHERE id = ?",
            (active["id"],),
        )
    return True


# ---------------------------------------------------------------------------
# Configuration submission
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def submit_config(conn: sqlite3.Connection, keywords: list[str]) -> int:
    """Validate and register a new configuration version, then wake the worker."""
    fields = validate_fields(keywords)
    # Imported lazily to avoid an import cycle (services imports this module).
    from .services import _BATCH_LOCK

    with _BATCH_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Every still-running older build is obsolete the moment a newer
            # configuration is accepted.
            conn.execute(
                "UPDATE search_config SET status = 'superseded', "
                "last_error = '已被更新配置取代' WHERE status = 'building'"
            )
            cur = conn.execute(
                "INSERT INTO search_config(fields_json, status) VALUES (?, 'building')",
                (json.dumps(fields),),
            )
            config_id = int(cur.lastrowid)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    request_rebuild()
    return config_id


def retry_config(conn: sqlite3.Connection, config_id: int) -> None:
    """Re-queue a failed build, provided no newer version has gone active."""
    from .services import _BATCH_LOCK

    with _BATCH_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT id, status FROM search_config WHERE id = ?", (config_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise SearchConfigError("配置版本不存在。")
            if row["status"] not in ("failed", "building"):
                conn.rollback()
                raise SearchConfigError("仅失败或中断的重建可以重试。")
            newer = conn.execute(
                "SELECT 1 FROM search_config WHERE id > ? AND status = 'active'",
                (config_id,),
            ).fetchone()
            if newer is not None:
                conn.execute(
                    "UPDATE search_config SET status = 'superseded' WHERE id = ?",
                    (config_id,),
                )
                conn.commit()
                raise SearchConfigError("该版本已被更新配置取代，无法重试。")
            conn.execute(
                "DELETE FROM search_index WHERE config_id = ?", (config_id,)
            )
            conn.execute(
                "DELETE FROM search_exclusion WHERE config_id = ?", (config_id,)
            )
            conn.execute(
                "UPDATE search_config SET status = 'building', last_error = NULL, "
                "total_products = 0, indexed_products = 0, excluded_count = 0 "
                "WHERE id = ?",
                (config_id,),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    request_rebuild()


# ---------------------------------------------------------------------------
# Background rebuild worker (one thread per process)
# ---------------------------------------------------------------------------

_worker_lock = threading.RLock()
_worker_thread: threading.Thread | None = None
_wakeup = threading.Event()
_stop = threading.Event()
_build_slot = threading.Lock()
# Recovery belongs to a service start: every time a different Flask app binds
# the worker, interrupted builds of that database are recovered once.
_recovered_for_app = None
# The most recently created app; the worker runs inside its application context.
_bound_app = None


def start_worker(app) -> None:
    global _worker_thread, _bound_app, _recovered_for_app, _stop
    with _worker_lock:
        if _bound_app is not app:
            _recovered_for_app = None
        _bound_app = app
        if _worker_thread is not None and _worker_thread.is_alive():
            _wakeup.set()
            return
        _stop.clear()
        _worker_thread = threading.Thread(
            target=_worker_loop, name="search-rebuild", daemon=True
        )
        _worker_thread.start()


def shutdown_worker(timeout: float = 5.0) -> None:
    _stop.set()
    _wakeup.set()
    thread = _worker_thread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=timeout)


def request_rebuild() -> None:
    _wakeup.set()


def _worker_loop() -> None:
    while not _stop.is_set():
        app = _bound_app
        if app is not None:
            try:
                with app.app_context():
                    _recover_interrupted_builds(app)
                    _process_next_build()
            except Exception:  # pragma: no cover - defensive, keep worker alive
                app.logger.exception("search rebuild worker error")
        if _stop.wait(IDLE_SCAN_SECONDS):
            break
        _wakeup.clear()


def _open_conn() -> sqlite3.Connection:
    from flask import current_app

    conn = sqlite3.connect(
        current_app.config["DB_PATH"], timeout=30, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _recover_interrupted_builds(app) -> None:
    """Re-queue interrupted/failed versions after a process restart.

    A version whose place has been taken by a newer active one is closed as
    superseded instead.  Partial index rows of retried versions are wiped so
    the rebuild starts from a clean slate.
    """
    global _recovered_for_app
    if _recovered_for_app is app:
        return
    conn = _open_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, status FROM search_config "
            "WHERE status IN ('building', 'failed') ORDER BY id"
        ).fetchall()
        for row in rows:
            newer_active = conn.execute(
                "SELECT 1 FROM search_config WHERE id > ? AND status = 'active'",
                (row["id"],),
            ).fetchone()
            if newer_active is not None:
                conn.execute(
                    "UPDATE search_config SET status = 'superseded' WHERE id = ?",
                    (row["id"],),
                )
                continue
            conn.execute(
                "DELETE FROM search_index WHERE config_id = ?", (row["id"],)
            )
            conn.execute(
                "DELETE FROM search_exclusion WHERE config_id = ?", (row["id"],)
            )
            note = "服务重启，自动重试重建"
            if row["status"] == "building":
                conn.execute(
                    "UPDATE search_config SET status = 'building', "
                    "last_error = ?, total_products = 0, indexed_products = 0, "
                    "excluded_count = 0 WHERE id = ?",
                    (note, row["id"]),
                )
            else:
                conn.execute(
                    "UPDATE search_config SET status = 'building', last_error = ? "
                    "WHERE id = ?",
                    (note, row["id"]),
                )
        conn.commit()
        _recovered_for_app = app
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _claim_next_build(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Claim the oldest pending version that is not obsolete."""
    conn.execute("BEGIN IMMEDIATE")
    candidate = conn.execute(
        "SELECT * FROM search_config WHERE status = 'building' ORDER BY id LIMIT 1"
    ).fetchone()
    if candidate is None:
        conn.rollback()
        return None
    newer_active = conn.execute(
        "SELECT 1 FROM search_config WHERE id > ? AND status = 'active'",
        (candidate["id"],),
    ).fetchone()
    if newer_active is not None:
        conn.execute(
            "UPDATE search_config SET status = 'superseded', "
            "last_error = '已被更新配置取代' WHERE id = ?",
            (candidate["id"],),
        )
        conn.execute(
            "DELETE FROM search_index WHERE config_id = ?", (candidate["id"],)
        )
        conn.execute(
            "DELETE FROM search_exclusion WHERE config_id = ?", (candidate["id"],)
        )
        conn.commit()
        return None
    conn.commit()
    return candidate


def _is_cancelled(conn: sqlite3.Connection, config_id: int) -> bool:
    row = conn.execute(
        "SELECT status FROM search_config WHERE id = ?", (config_id,)
    ).fetchone()
    return row is None or row["status"] != "building"


def _mark_failed(conn: sqlite3.Connection, config_id: int, exc: BaseException) -> None:
    detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    conn.execute(
        "UPDATE search_config SET status = 'failed', last_error = ? WHERE id = ?",
        (detail[:500], config_id),
    )


def _all_products(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT bf.id AS batch_file_id, bf.hospital_id, bf.sop_uid_old, "
        "bf.stored_name, b.batch_code "
        "FROM batch_file bf JOIN batch b ON b.id = bf.created_batch_id "
        "WHERE b.status = 'published' ORDER BY bf.id"
    ).fetchall()


def _missing_products(conn: sqlite3.Connection, config_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT bf.id AS batch_file_id, bf.hospital_id, bf.sop_uid_old, "
        "bf.stored_name, b.batch_code "
        "FROM batch_file bf JOIN batch b ON b.id = bf.created_batch_id "
        "WHERE b.status = 'published' AND bf.id NOT IN ("
        "  SELECT batch_file_id FROM search_index WHERE config_id = ? "
        "  UNION "
        "  SELECT batch_file_id FROM search_exclusion WHERE config_id = ?) "
        "ORDER BY bf.id",
        (config_id, config_id),
    ).fetchall()


def _scan_products(conn: sqlite3.Connection, config_id: int, fields: list[str],
                   products: list[sqlite3.Row], logger) -> bool:
    """Index the given products in chunks; return False if cancelled."""
    from .services import published_path

    indexed = excluded = 0
    for start in range(0, len(products), CHUNK_SIZE):
        if _is_cancelled(conn, config_id):
            return False
        chunk = products[start:start + CHUNK_SIZE]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for product in chunk:
                path = published_path(product["batch_code"], product["stored_name"])
                try:
                    ds = pydicom.dcmread(path)
                    outcome = _index_one(
                        conn, config_id, fields, product["hospital_id"],
                        product["batch_file_id"], product["sop_uid_old"], ds,
                    )
                except Exception as exc:  # unreadable product: explain, keep going
                    conn.execute(
                        "INSERT OR IGNORE INTO search_exclusion(config_id, "
                        "hospital_id, batch_file_id, sop_uid_old, field_keyword, "
                        "reason, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (config_id, product["hospital_id"], product["batch_file_id"],
                         product["sop_uid_old"], fields[0], "read_error",
                         str(exc)[:200]),
                    )
                    outcome = "excluded"
                if outcome == "indexed":
                    indexed += 1
                else:
                    excluded += 1
            conn.execute(
                "UPDATE search_config SET indexed_products = indexed_products + ?, "
                "excluded_count = excluded_count + ? WHERE id = ?",
                (indexed, excluded, config_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        indexed = excluded = 0
        logger.info("search config %s: scanned %d/%d products",
                    config_id, min(start + CHUNK_SIZE, len(products)), len(products))
    return True


def _process_next_build() -> None:
    if not _build_slot.acquire(blocking=False):
        return
    from flask import current_app

    logger = current_app.logger
    conn = _open_conn()
    try:
        candidate = _claim_next_build(conn)
        if candidate is None:
            return
        config_id = candidate["id"]
        fields = parse_fields(candidate["fields_json"])
        logger.info("rebuilding search index for config %s fields=%s",
                    config_id, fields)
        try:
            products = _all_products(conn)
            if not _scan_products(conn, config_id, fields, products, logger):
                logger.info("config %s superseded during scan; aborting", config_id)
                _discard_build(conn, config_id)
                return

            # Final coverage + atomic switch under the batch lock: no upload can
            # commit in this window, so this single catch-up pass is authoritative.
            from .services import _BATCH_LOCK, published_path

            with _BATCH_LOCK:
                conn.execute("BEGIN IMMEDIATE")
                if _is_cancelled(conn, config_id):
                    conn.rollback()
                    _discard_build(conn, config_id)
                    return
                newer_active = conn.execute(
                    "SELECT 1 FROM search_config WHERE id > ? AND status = 'active'",
                    (config_id,),
                ).fetchone()
                if newer_active is not None:
                    conn.rollback()
                    _discard_build(conn, config_id)
                    return

                # Catch products uploaded while the long scan was running.
                missing = _missing_products(conn, config_id)
                for product in missing:
                    path = published_path(product["batch_code"], product["stored_name"])
                    try:
                        ds = pydicom.dcmread(path)
                        _index_one(
                            conn, config_id, fields, product["hospital_id"],
                            product["batch_file_id"], product["sop_uid_old"], ds,
                        )
                    except Exception as exc:
                        conn.execute(
                            "INSERT OR IGNORE INTO search_exclusion(config_id, "
                            "hospital_id, batch_file_id, sop_uid_old, field_keyword, "
                            "reason, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (config_id, product["hospital_id"], product["batch_file_id"],
                             product["sop_uid_old"], fields[0], "read_error",
                             str(exc)[:200]),
                        )

                total = conn.execute(
                    "SELECT COUNT(*) FROM batch_file bf JOIN batch b "
                    "ON b.id = bf.created_batch_id WHERE b.status = 'published'"
                ).fetchone()[0]
                indexed = conn.execute(
                    "SELECT COUNT(*) FROM search_index WHERE config_id = ?",
                    (config_id,),
                ).fetchone()[0]
                excluded = conn.execute(
                    "SELECT COUNT(*) FROM search_exclusion WHERE config_id = ?",
                    (config_id,),
                ).fetchone()[0]
                if total != indexed + excluded:
                    raise RuntimeError(
                        f"index coverage mismatch: {total} products vs "
                        f"{indexed} indexed + {excluded} excluded"
                    )
                conn.execute(
                    "UPDATE search_config SET status = 'superseded' "
                    "WHERE status = 'active'"
                )
                conn.execute(
                    "UPDATE search_config SET status = 'active', total_products = ?, "
                    "indexed_products = ?, excluded_count = ?, activated_at = ?, "
                    "last_error = NULL WHERE id = ?",
                    (total, indexed, excluded, _utc_now(), config_id),
                )
                conn.commit()
            logger.info("search config %s is now active (%d indexed, %d excluded)",
                        config_id, indexed, excluded)
        except Exception as exc:
            logger.exception("search rebuild failed for config %s", config_id)
            conn.execute("BEGIN IMMEDIATE")
            try:
                if not _is_cancelled(conn, config_id):
                    _mark_failed(conn, config_id, exc)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    finally:
        conn.close()
        _build_slot.release()


def _discard_build(conn: sqlite3.Connection, config_id: int) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM search_index WHERE config_id = ?", (config_id,))
        conn.execute(
            "DELETE FROM search_exclusion WHERE config_id = ?", (config_id,)
        )
        conn.execute(
            "UPDATE search_config SET total_products = 0, indexed_products = 0, "
            "excluded_count = 0 WHERE id = ? AND status != 'superseded'",
            (config_id,),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Read-side queries used by the browser
# ---------------------------------------------------------------------------

def get_active_config(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM search_config WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def get_config(conn: sqlite3.Connection, config_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM search_config WHERE id = ?", (config_id,)
    ).fetchone()


def list_configs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM search_config ORDER BY id DESC"
    ).fetchall()


def list_exclusions(conn: sqlite3.Connection, config_id: int,
                    limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT e.*, h.code AS hospital_code FROM search_exclusion e "
        "JOIN hospital h ON h.id = e.hospital_id "
        "WHERE e.config_id = ? ORDER BY e.id LIMIT ?",
        (config_id, limit),
    ).fetchall()


def search_products(conn: sqlite3.Connection, config_id: int, hospital_id: int,
                    bounds: list[tuple[float | None, float | None]]
                    ) -> list[sqlite3.Row]:
    """Closed-interval numeric search, deduplicated by SOP via the unique index."""
    sql = (
        "SELECT si.sop_uid_old, si.value_1, si.value_2, si.value_3, "
        "si.batch_file_id, bf.original_filename, bf.stored_name, "
        "b.batch_code, h.code AS hospital_code "
        "FROM search_index si "
        "JOIN batch_file bf ON bf.id = si.batch_file_id "
        "JOIN batch b ON b.id = bf.created_batch_id "
        "JOIN hospital h ON h.id = si.hospital_id "
        "WHERE si.config_id = ? AND si.hospital_id = ?"
    )
    params: list = [config_id, hospital_id]
    for index, (low, high) in enumerate(bounds, start=1):
        if low is not None:
            sql += f" AND si.value_{index} >= ?"
            params.append(low)
        if high is not None:
            sql += f" AND si.value_{index} <= ?"
            params.append(high)
    sql += " ORDER BY si.id"
    return conn.execute(sql, params).fetchall()
