"""Tests for numeric-tag search: config validation, extraction, rebuild lifecycle.

The background rebuild worker is deliberately disabled in these tests: rebuilds
are driven explicitly through ``search._process_next_build`` so timing does not
make the suite flaky.
"""
from __future__ import annotations

import io
import math
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import pydicom

from app import create_app, search
from app.search import SearchConfigError, extract_numeric, validate_fields
from tests.dicom_factory import make_ct_file


def make_app(data_dir: str):
    app = create_app(
        {
            "TESTING": True,
            "DICOM_DATA_DIR": data_dir,
            "STAGING_DIR": os.path.join(data_dir, "staging"),
            "PUBLISHED_DIR": os.path.join(data_dir, "published"),
            "DB_PATH": os.path.join(data_dir, "registry.db"),
        }
    )
    search.shutdown_worker()
    return app


def upload_ct(client, hospital: str, path: str, filename: str | None = None):
    with open(path, "rb") as handle:
        data = {
            "hospital_code": hospital,
            "dicom_files": [(io.BytesIO(handle.read()), filename or os.path.basename(path))],
        }
    return client.post(
        "/upload", data=data, content_type="multipart/form-data",
        follow_redirects=False,
    )


class ConfigValidationTests(unittest.TestCase):
    def test_accepts_standard_numeric_keywords(self):
        self.assertEqual(
            validate_fields(["slicethickness", "KVP"]),
            ["SliceThickness", "KVP"],
        )

    def test_rejects_unknown_private_and_non_numeric_tags(self):
        for bad in (["DoesNotExist"], ["ConvolutionKernel"], ["PixelData"],
                    ["TransferSyntaxUID"], ["PatientName"], ["PixelSpacing"],
                    ["ImagePositionPatient"]):
            with self.assertRaises(SearchConfigError):
                validate_fields(bad)

    def test_rejects_wrong_count_and_duplicates(self):
        with self.assertRaises(SearchConfigError):
            validate_fields([])
        with self.assertRaises(SearchConfigError):
            validate_fields(["SliceThickness"] * 4)
        with self.assertRaises(SearchConfigError):
            validate_fields(["SliceThickness", "SliceThickness"])


class ExtractionTests(unittest.TestCase):
    def _ds(self, invalid=None, numeric=None):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "f.dcm")
        make_ct_file(
            path,
            numeric_tags=numeric or {"SliceThickness": 2.5, "KVP": 130.0},
            invalid_tags=invalid or {},
        )
        return pydicom.dcmread(path)

    def test_single_finite_number_hits(self):
        ds = self._ds()
        value, reason = extract_numeric(ds, "SliceThickness")
        self.assertIsNone(reason)
        self.assertEqual(value, 2.5)

    def test_missing_empty_non_numeric_non_finite_multi(self):
        ds = self._ds()
        _, reason = extract_numeric(ds, "ReconstructionDiameter")  # absent
        self.assertEqual(reason, "missing")

        for scenario, expected in (
            ("empty", "empty"),
            ("non_numeric", "non_numeric"),
            ("nan", "not_finite"),
            ("inf", "not_finite"),
            ("multi", "multi_value"),
        ):
            ds_bad = self._ds(invalid={"SliceThickness": scenario})
            value, reason = extract_numeric(ds_bad, "SliceThickness")
            self.assertIsNone(value)
            self.assertEqual(reason, expected, scenario)


class SearchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmp, "data")
        self.upload_dir = os.path.join(self.tmp, "uploads")
        os.makedirs(self.data_dir)
        os.makedirs(self.upload_dir)
        self.app = make_app(self.data_dir)
        self.client = self.app.test_client()

    def tearDown(self):
        search.shutdown_worker()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _publish_variants(self, hospital="H1"):
        """Four products covering hits and every exclusion reason."""
        specs = [
            ("ok1", {"SliceThickness": 1.0, "KVP": 120.0}, {}),
            ("ok2", {"SliceThickness": 3.0, "KVP": 80.0}, {}),
            ("missing", {"KVP": 120.0}, {}),  # no SliceThickness
            ("bad", {}, {"SliceThickness": "non_numeric"}),
            ("empty", {"KVP": 120.0}, {"SliceThickness": "empty"}),
            ("nan", {"KVP": 120.0}, {"SliceThickness": "nan"}),
            ("multi", {"KVP": 120.0}, {"SliceThickness": "multi"}),
        ]
        paths = []
        for name, numeric, invalid in specs:
            path = os.path.join(self.upload_dir, f"{name}.dcm")
            make_ct_file(path, numeric_tags=numeric, invalid_tags=invalid)
            paths.append(path)
            resp = upload_ct(self.client, hospital, path)
            self.assertEqual(resp.status_code, 302, resp.data[:300])
        return paths

    def _submit(self, fields):
        resp = self.client.post(
            "/search/config",
            data={"fields": fields},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            conn = search._open_conn()
            try:
                search._process_next_build()
                return conn.execute(
                    "SELECT * FROM search_config ORDER BY id DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()

    def test_rebuild_indexes_hits_and_records_exclusion_reasons(self):
        self._publish_variants()
        row = self._submit(["SliceThickness", "KVP"])
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["total_products"], 7)
        self.assertEqual(row["indexed_products"], 2)
        self.assertEqual(row["excluded_count"], 5)

        with self.app.app_context():
            conn = search._open_conn()
            try:
                reasons = {
                    r["sop_uid_old"]: (r["field_keyword"], r["reason"])
                    for r in search.list_exclusions(conn, row["id"], limit=100)
                }
            finally:
                conn.close()
        # Every non-hit product appears once with an explanatory reason.
        self.assertEqual(len(reasons), 5)
        reason_codes = {code for _, code in reasons.values()}
        self.assertEqual(
            reason_codes,
            {"missing", "empty", "non_numeric", "not_finite", "multi_value"},
        )

    def test_closed_interval_search_and_sop_dedup(self):
        self._publish_variants()
        row = self._submit(["SliceThickness", "KVP"])

        def query(**params):
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            resp = self.client.get(
                f"/search?v={row['id']}&do=1&hospital=H1&{qs}"
            )
            self.assertEqual(resp.status_code, 200)
            return resp.get_data(as_text=True)

        # Closed interval [1, 1] matches the 1.0 slice; boundary is inclusive.
        body = query(min_0=1, max_0=1)
        self.assertIn("1 个 SOP", body)
        self.assertIn("/products/", body)
        # Two-sided interval on the second field: KVP <= 100 keeps only ok2.
        body = query(min_0=0, max_0=5, min_1=0, max_1=100)
        self.assertIn("1 个 SOP", body)
        # Open-ended side: only a lower bound.
        body = query(min_0=2.5)
        self.assertIn("1 个 SOP", body)
        # No match.
        body = query(min_0=99)
        self.assertIn("没有命中", body)
        # Invalid bounds.
        body = query(min_0=5, max_0=1)
        self.assertIn("下限不能大于上限", body)
        body = query(min_0="abc")
        self.assertIn("不是数值", body)

    def test_switch_is_atomic_and_old_form_rejected(self):
        self._publish_variants()
        first = self._submit(["SliceThickness"])
        self.assertEqual(first["status"], "active")

        # A second configuration builds against the same products.
        second = self._submit(["KVP"])
        with self.app.app_context():
            conn = search._open_conn()
            try:
                statuses = {
                    r["id"]: r["status"]
                    for r in conn.execute("SELECT id, status FROM search_config")
                }
            finally:
                conn.close()
        self.assertEqual(statuses[first["id"]], "superseded")
        self.assertEqual(statuses[second["id"]], "active")

        # Form carrying the old version id is rejected; new form works.
        old = self.client.get(
            f"/search?v={first['id']}&do=1&hospital=H1&min_0=1&max_0=1"
        )
        self.assertIn("旧表单已拒绝", old.get_data(as_text=True))
        fresh = self.client.get(
            f"/search?v={second['id']}&do=1&hospital=H1"
        )
        self.assertEqual(fresh.status_code, 200)
        self.assertNotIn("旧表单已拒绝", fresh.get_data(as_text=True))

    def test_products_uploaded_during_scan_are_not_missed(self):
        # Publish one product, then start a build whose scan pauses after the
        # first product so an upload can land while the scan is in progress.
        first_path = os.path.join(self.upload_dir, "early.dcm")
        make_ct_file(first_path, numeric_tags={"SliceThickness": 2.0})
        self.assertEqual(upload_ct(self.client, "H1", first_path).status_code, 302)

        real_scan = search._scan_products
        scanned = {"count": 0}

        def slow_scan(conn, config_id, fields, products, logger):
            scanned["count"] += 1
            result = real_scan(conn, config_id, fields, products, logger)
            if scanned["count"] == 1:
                late_path = os.path.join(self.upload_dir, "late.dcm")
                make_ct_file(late_path, numeric_tags={"SliceThickness": 4.0})
                resp = upload_ct(self.client, "H1", late_path)
                self.assertEqual(resp.status_code, 302)
            return result

        self.client.post("/search/config", data={"fields": "SliceThickness"})
        with self.app.app_context(), mock.patch.object(
            search, "_scan_products", side_effect=slow_scan
        ):
            conn = search._open_conn()
            try:
                search._process_next_build()
                row = conn.execute(
                    "SELECT * FROM search_config WHERE status = 'active'"
                ).fetchone()
                hits = conn.execute(
                    "SELECT COUNT(*) FROM search_index WHERE config_id = ?",
                    (row["id"],),
                ).fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(row["total_products"], 2)
        self.assertEqual(hits, 2, "product uploaded mid-rebuild must be indexed")

    def test_upload_after_active_config_is_indexed_incrementally(self):
        row = self._submit(["SliceThickness"])
        path = os.path.join(self.upload_dir, "inc.dcm")
        make_ct_file(path, numeric_tags={"SliceThickness": 7.5})
        self.assertEqual(upload_ct(self.client, "H1", path).status_code, 302)

        with self.app.app_context():
            conn = search._open_conn()
            try:
                hit = conn.execute(
                    "SELECT value_1, total_products, indexed_products "
                    "FROM search_index si JOIN search_config sc "
                    "ON sc.id = si.config_id WHERE sc.id = ?",
                    (row["id"],),
                ).fetchone()
                self.assertEqual(hit["value_1"], 7.5)
                self.assertEqual(hit["total_products"], 1)
                self.assertEqual(hit["indexed_products"], 1)
            finally:
                conn.close()
        # Searchable immediately through the browser.
        resp = self.client.get(
            f"/search?v={row['id']}&do=1&hospital=H1&min_0=7&max_0=8"
        )
        self.assertIn("1 个 SOP", resp.get_data(as_text=True))

    def test_reused_product_is_not_double_counted(self):
        path = os.path.join(self.upload_dir, "dup.dcm")
        make_ct_file(path, numeric_tags={"SliceThickness": 2.0})
        upload_ct(self.client, "H1", path)
        # Same bytes + same SOP re-upload: reuse, no extra batch_file row.
        upload_ct(self.client, "H1", path, filename="dup-again.dcm")
        row = self._submit(["SliceThickness"])
        self.assertEqual(row["total_products"], 1)
        self.assertEqual(row["indexed_products"], 1)

    def test_failed_upload_leaves_no_index(self):
        self._submit(["SliceThickness"])
        # An invalid (non-CT) file is rejected; no product and no index rows.
        bad_path = os.path.join(self.upload_dir, "bad.dcm")
        make_ct_file(bad_path)
        ds = pydicom.dcmread(bad_path)
        ds.Modality = "MR"
        ds.save_as(bad_path, write_like_original=False)
        resp = upload_ct(self.client, "H1", bad_path)
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            conn = search._open_conn()
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM batch_file").fetchone()[0], 0
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0], 0
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM search_exclusion").fetchone()[0],
                    0,
                )
            finally:
                conn.close()

    def test_superseded_build_never_activates(self):
        first_path = os.path.join(self.upload_dir, "p.dcm")
        make_ct_file(first_path, numeric_tags={"SliceThickness": 2.0})
        upload_ct(self.client, "H1", first_path)

        # Submit two configs without building; the older building version must
        # be discarded when the worker finally runs.
        self.client.post("/search/config", data={"fields": "SliceThickness"})
        self.client.post("/search/config", data={"fields": "KVP"})
        with self.app.app_context():
            conn = search._open_conn()
            try:
                search._process_next_build()
                rows = conn.execute(
                    "SELECT id, status FROM search_config ORDER BY id"
                ).fetchall()
            finally:
                conn.close()
        self.assertEqual(rows[0]["status"], "superseded")
        self.assertEqual(rows[1]["status"], "active")

    def test_failed_rebuild_keeps_old_config_and_retry_succeeds(self):
        first_path = os.path.join(self.upload_dir, "p.dcm")
        make_ct_file(first_path, numeric_tags={"SliceThickness": 2.0})
        upload_ct(self.client, "H1", first_path)
        first = self._submit(["SliceThickness"])

        # New build fails midway: old active version must stay active.
        self.client.post("/search/config", data={"fields": "KVP"})
        with self.app.app_context(), mock.patch.object(
            search, "_scan_products", side_effect=RuntimeError("disk boom")
        ):
            conn = search._open_conn()
            try:
                search._process_next_build()
                rows = {
                    r["id"]: r["status"]
                    for r in conn.execute("SELECT id, status FROM search_config")
                }
                failed_id = max(rows)
                self.assertEqual(rows[first["id"]], "active")
                self.assertEqual(rows[failed_id], "failed")
                error = conn.execute(
                    "SELECT last_error FROM search_config WHERE id = ?",
                    (failed_id,),
                ).fetchone()["last_error"]
                self.assertIn("disk boom", error)
            finally:
                conn.close()

        # Retry via the service and it completes.
        with self.app.app_context():
            conn = search._open_conn()
            try:
                search.retry_config(conn, failed_id)
            finally:
                conn.close()
        with self.app.app_context():
            conn = search._open_conn()
            try:
                search._process_next_build()
                final = conn.execute(
                    "SELECT status FROM search_config WHERE id = ?", (failed_id,)
                ).fetchone()
                old = conn.execute(
                    "SELECT status FROM search_config WHERE id = ?", (first["id"],)
                ).fetchone()
            finally:
                conn.close()
        self.assertEqual(final["status"], "active")
        self.assertEqual(old["status"], "superseded")

    def test_restart_retries_interrupted_build_and_keeps_latest(self):
        first_path = os.path.join(self.upload_dir, "p.dcm")
        make_ct_file(first_path, numeric_tags={"SliceThickness": 2.0, "KVP": 120.0})
        upload_ct(self.client, "H1", first_path)

        # Simulate a build interrupted mid-flight (status stuck at building).
        self.client.post("/search/config", data={"fields": "SliceThickness"})
        db_path = os.path.join(self.data_dir, "registry.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO search_index(config_id, hospital_id, batch_file_id, "
            "sop_uid_old, value_1) "
            "SELECT 1, hospital_id, id, sop_uid_old, 1 FROM batch_file"
        )
        conn.commit()
        conn.close()

        # A new app instance on the same volume recovers and rebuilds itself.
        app2 = make_app(self.data_dir)
        try:
            with app2.app_context():
                conn2 = search._open_conn()
                try:
                    search._process_next_build()
                    row = conn2.execute(
                        "SELECT * FROM search_config WHERE id = 1"
                    ).fetchone()
                finally:
                    conn2.close()
            self.assertEqual(row["status"], "active")
            # Partial rows from the interrupted attempt were rebuilt cleanly.
            conn2 = sqlite3.connect(db_path)
            try:
                count = conn2.execute(
                    "SELECT COUNT(*) FROM search_index WHERE config_id = 1"
                ).fetchone()[0]
            finally:
                conn2.close()
            self.assertEqual(count, 1)
        finally:
            search.shutdown_worker()

    def test_late_old_rebuild_cannot_replace_newer_config(self):
        path = os.path.join(self.upload_dir, "p.dcm")
        make_ct_file(path, numeric_tags={"SliceThickness": 2.0, "KVP": 120.0})
        upload_ct(self.client, "H1", path)

        # Config 1 builds and activates normally.
        first = self._submit(["SliceThickness"])
        # Config 2 builds; pretend config 1's build "finishes late" by invoking
        # its flip path directly. The newer-active guard must reject it.
        second = self._submit(["KVP"])
        with self.app.app_context():
            conn = search._open_conn()
            try:
                # Revert config 1 to building to simulate a delayed old build.
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE search_config SET status = 'building' WHERE id = ?",
                    (first["id"],),
                )
                conn.commit()
                search._process_next_build()
                statuses = {
                    r["id"]: r["status"]
                    for r in conn.execute("SELECT id, status FROM search_config")
                }
            finally:
                conn.close()
        self.assertEqual(statuses[first["id"]], "superseded")
        self.assertEqual(statuses[second["id"]], "active")

    def test_existing_volume_uids_and_bytes_unchanged(self):
        path = os.path.join(self.upload_dir, "p.dcm")
        make_ct_file(path, numeric_tags={"SliceThickness": 2.0})
        upload_ct(self.client, "H1", path)
        db_path = os.path.join(self.data_dir, "registry.db")
        conn = sqlite3.connect(db_path)
        before_mappings = conn.execute(
            "SELECT old_uid, new_uid FROM uid_mapping ORDER BY old_uid"
        ).fetchall()
        before_files = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT stored_name, file_sha256 FROM batch_file"
            )
        }
        conn.close()

        self._submit(["SliceThickness"])  # rebuild touches only search tables

        conn = sqlite3.connect(db_path)
        try:
            after_mappings = conn.execute(
                "SELECT old_uid, new_uid FROM uid_mapping ORDER BY old_uid"
            ).fetchall()
            after_files = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT stored_name, file_sha256 FROM batch_file"
                )
            }
        finally:
            conn.close()
        self.assertEqual(before_mappings, after_mappings)
        self.assertEqual(before_files, after_files)

    def test_product_download_link_works(self):
        row = self._submit(["SliceThickness"])
        path = os.path.join(self.upload_dir, "dl.dcm")
        make_ct_file(path, numeric_tags={"SliceThickness": 2.0})
        upload_ct(self.client, "H1", path)
        with self.app.app_context():
            conn = search._open_conn()
            try:
                indexed = conn.execute(
                    "SELECT batch_file_id FROM search_index WHERE config_id = ?",
                    (row["id"],),
                ).fetchone()
            finally:
                conn.close()
        resp = self.client.get(f"/products/{indexed['batch_file_id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/dicom")
        # Unknown product -> 404.
        self.assertEqual(self.client.get("/products/999999").status_code, 404)

    def test_browser_rejects_invalid_config_form(self):
        resp = self.client.post(
            "/search/config", data={"fields": "ConvolutionKernel"}
        )
        self.assertEqual(resp.status_code, 302)
        follow = self.client.get(resp.headers["Location"])
        self.assertIn("检索配置被拒绝", follow.get_data(as_text=True))

    def test_search_requires_hospital(self):
        row = self._submit(["SliceThickness"])
        resp = self.client.get(f"/search?v={row['id']}&do=1")
        self.assertIn("请选择医院", resp.get_data(as_text=True))

    def test_search_is_isolated_per_hospital(self):
        # Same SOP UID at two hospitals yields two distinct index rows.
        shared_sop = "1.2.3.456.789.1"
        for hospital in ("HA", "HB"):
            path = os.path.join(self.upload_dir, f"{hospital}.dcm")
            make_ct_file(
                path, sop_uid=shared_sop,
                numeric_tags={"SliceThickness": 2.0, "KVP": 120.0},
            )
            self.assertEqual(
                upload_ct(self.client, hospital, path).status_code, 302
            )
        row = self._submit(["SliceThickness", "KVP"])
        with self.app.app_context():
            conn = search._open_conn()
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM search_index WHERE config_id = ?",
                    (row["id"],),
                ).fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(count, 2, "SOP must be indexed once per hospital")

        body_a = self.client.get(
            f"/search?v={row['id']}&do=1&hospital=HA"
        ).get_data(as_text=True)
        body_b = self.client.get(
            f"/search?v={row['id']}&do=1&hospital=HB"
        ).get_data(as_text=True)
        self.assertIn("1 个 SOP", body_a)
        self.assertIn("1 个 SOP", body_b)

    def test_no_active_config_means_no_query_form(self):
        resp = self.client.get("/search")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("当前没有生效中的检索配置", resp.get_data(as_text=True))
        # Even with a v parameter, no query is executed.
        resp = self.client.get("/search?v=1&do=1&hospital=H1")
        self.assertNotIn("个 SOP", resp.get_data(as_text=True))

    def test_unknown_config_version_returns_404(self):
        self.assertEqual(self.client.get("/search?config=999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
