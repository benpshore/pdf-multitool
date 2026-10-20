import importlib.util
import json
import shutil
import sys
import unittest
from pathlib import Path

import pytest

from tests import test_extended_http, test_server
from tests.test_table_structure import CONTRACTS, rendered_grid

ROOT = Path(__file__).resolve().parents[1]


class StructuredTableHTTPTests(unittest.TestCase):
    setUp = test_server.RealHTTPTests.setUp
    tearDown = test_server.RealHTTPTests.tearDown
    request = test_server.RealHTTPTests.request
    upload = test_server.RealHTTPTests.upload
    wait_job = test_extended_http.AdditionalHTTPTests.wait_job

    def test_actual_downloads_keep_wrapped_and_empty_cell_positions(self):
        self.pdf = (
            ROOT / "experiments/structured_cells_v1/fixtures/wrapped_ruled_reverse.pdf"
        ).read_bytes()
        source = self.upload()
        expected = CONTRACTS["wrapped_ruled_reverse"]["authored_cells"]
        for operation, kind in (("textlayout", "txt"), ("markdown", "md"), ("pdfhtml", "html")):
            status, _, body = self.request(
                "POST",
                "/api/jobs",
                {
                    "operation": operation,
                    "files": [source["id"]],
                    "options": {},
                },
            )
            self.assertEqual(status, 202, body)
            job = self.wait_job(json.loads(body)["id"])
            self.assertEqual(job["state"], "completed", job)
            primary = next(r for r in job["results"] if r["name"].endswith("." + kind))
            status, _, content = self.request("GET", primary["url"])
            self.assertEqual(status, 200)
            self.assertEqual(rendered_grid(content.decode(), kind, 3), expected)
            report = next(r for r in job["results"] if r["name"].endswith(".tables.json"))
            status, _, content = self.request("GET", report["url"])
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(content)["pages"][0]["tables"][0]["rows"], expected)
        self.assertEqual(self.request("GET", f"/api/files/{source['id']}/original")[2], self.pdf)


@pytest.mark.skipif(
    not importlib.util.find_spec("playwright")
    or not shutil.which("chromium")
    or not shutil.which("pandoc"),
    reason="Requires playwright, system chromium and pandoc",
)
def test_gui_structured_cells_and_explicit_remaining_failures(tmp_path, monkeypatch):
    from scripts.qualify_table_structure import main

    output = tmp_path / "structured-cells"
    monkeypatch.setattr(sys, "argv", ["qualify_table_structure", "--output", str(output)])
    main()
    report = json.loads((output / "report.json").read_text())
    assert len(report["scenarios"]) == 36
    assert report["rendered_cell_matches"] == 21
    assert report["unresolved_table_outputs"] == 12
    assert all(r["ada_review"] == "not_reviewed" for r in report["scenarios"])
