import importlib.util
import json
import shutil
import sys
import unittest
from pathlib import Path

import pytest

from experiments.table_prose_current_v1.accept import export_body
from tests import test_extended_http, test_server

ROOT = Path(__file__).resolve().parents[1]


class ReadingOrderHTTPTests(unittest.TestCase):
    setUp = test_server.RealHTTPTests.setUp
    tearDown = test_server.RealHTTPTests.tearDown
    request = test_server.RealHTTPTests.request
    upload = test_server.RealHTTPTests.upload
    wait_job = test_extended_http.AdditionalHTTPTests.wait_job

    def test_policy_validation_download_and_original(self):
        self.pdf = (
            ROOT / "experiments/representation_v1/fixtures/table_row_operators.pdf"
        ).read_bytes()
        source = self.upload()
        request = {
            "operation": "textlayout",
            "files": [source["id"]],
            "options": {"reading_order": True},
        }
        self.assertEqual(self.request("POST", "/api/jobs", request)[0], 400)
        self.assertEqual(len(self.app.jobs), 0)
        for operation, suffix in (("textlayout", "txt"), ("markdown", "md"), ("pdfhtml", "html")):
            request.update(operation=operation, options={"reading_order": "rows"})
            status, _, body = self.request("POST", "/api/jobs", request)
            self.assertEqual(status, 202, body)
            job = self.wait_job(json.loads(body)["id"])
            self.assertEqual(job["state"], "completed", job)
            result = next(r for r in job["results"] if r["name"].endswith("." + suffix))
            status, _, content = self.request("GET", result["url"])
            self.assertEqual(status, 200)
            directory = self.root / operation
            directory.mkdir()
            (directory / ("export." + suffix)).write_bytes(content)
            self.assertEqual(export_body(directory, suffix), "A1 B1 A2 B2 A3 B3")
            self.assertTrue(
                any("explicitly selected as rows" in warning for warning in job["warnings"])
            )
        self.assertEqual(self.request("GET", f"/api/files/{source['id']}/original")[2], self.pdf)


@pytest.mark.skipif(
    not importlib.util.find_spec("playwright") or not shutil.which("chromium"),
    reason="Requires playwright and system chromium",
)
def test_real_gui_policy_and_unchanged_default_evidence(tmp_path, monkeypatch):
    from scripts.qualify_reading_order import main

    output = tmp_path / "reading-order"
    monkeypatch.setattr(sys, "argv", ["qualify_reading_order", "--output", str(output)])
    main()
    report = json.loads((output / "report.json").read_text())
    assert len(report["scenarios"]) == 21
    assert report["default_table_failure_reproduced"]
    assert all(
        row["matches_selected_policy"] is True
        for row in report["scenarios"]
        if row["mode"] != "auto"
    )
    assert all(
        row["matches_selected_policy"] is None
        and row["known_default_semantic_failure"]
        and row["matches_original_table_oracle"] is False
        for row in report["scenarios"]
        if row["mode"] == "auto"
    )
    assert all(row["ada_review"] == "not_reviewed" for row in report["scenarios"])
