"""Additional operation contracts over real loopback HTTP, including recovery."""

import json
import time
import unittest

from scripts.generate_openapi import build
from tests import test_server


class AdditionalHTTPTests(unittest.TestCase):
    setUp = test_server.RealHTTPTests.setUp
    tearDown = test_server.RealHTTPTests.tearDown
    request = test_server.RealHTTPTests.request
    upload = test_server.RealHTTPTests.upload

    def wait_job(self, job_id):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            status, _, payload = self.request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(status, 200)
            snapshot = json.loads(payload)
            if snapshot["state"] not in {"running", "queued"}:
                return snapshot
            time.sleep(0.03)
        self.fail("Job did not terminate")

    def test_create_without_upload_and_download_original_independently(self):
        source = self.upload()
        status, _, body = self.request(
            "POST",
            "/api/jobs",
            {
                "operation": "create",
                "files": [],
                "options": {"content": "Created through HTTP"},
            },
        )
        self.assertEqual(status, 202, body)
        job = self.wait_job(json.loads(body)["id"])
        self.assertEqual(job["state"], "completed", job)
        status, headers, content = self.request("GET", job["results"][0]["url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertTrue(content.startswith(b"%PDF"))
        self.assertEqual(self.request("GET", f"/api/files/{source['id']}/original")[2], self.pdf)
        self.assertNotIn("path", job["results"][0])

    def test_schema_errors_and_backend_failure_allow_recovery(self):
        source = self.upload()
        request = {
            "operation": "optimize",
            "files": [source["id"]],
            "options": {"compression_level": 50},
        }
        status, _, payload = self.request("POST", "/api/jobs", request)
        self.assertEqual(status, 400)
        self.assertIn("compression_level", json.loads(payload)["error"])
        self.assertEqual(len(self.app.jobs), 0)
        request.update(operation="ocr_tesseract", options={})
        status, _, payload = self.request("POST", "/api/jobs", request)
        self.assertEqual(status, 202)
        failed = self.wait_job(json.loads(payload)["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["results"], [])
        request.update(operation="geometry", options={})
        status, _, payload = self.request("POST", "/api/jobs", request)
        self.assertEqual(status, 202)
        good = self.wait_job(json.loads(payload)["id"])
        self.assertEqual(good["state"], "completed", good)

    def test_backpressure_while_an_engine_owns_the_gate(self):
        source = self.upload()
        self.app.engine_lock.acquire()
        try:
            request = {"operation": "optimize", "files": [source["id"]], "options": {}}
            first, _, body = self.request("POST", "/api/jobs", request)
            self.assertEqual(first, 202)
            job_id = json.loads(body)["id"]
            second, _, payload = self.request("POST", "/api/jobs", request)
            self.assertEqual(second, 400)
            self.assertIn("active job", json.loads(payload)["error"])
            self.assertEqual(len(self.app.jobs), 1)
            self.request("POST", f"/api/jobs/{job_id}/cancel", {})
        finally:
            self.app.engine_lock.release()
        self.assertEqual(self.wait_job(job_id)["state"], "cancelled")

    def test_capabilities_and_generated_openapi_describe_real_routes(self):
        from pathlib import Path

        self.assertEqual(json.loads(Path("openapi.yaml").read_text()), build())
        status, _, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        capabilities = json.loads(payload)["capabilities"]
        self.assertIn("tables", capabilities["operations"])
        self.assertEqual(capabilities["operations"]["create"]["inputs"], 0)
        self.assertTrue(any(g["feature"] == "Curved-page dewarping" for g in capabilities["gaps"]))
        self.assertIn("/api/jobs/{job_id}/cancel", build()["paths"])
        self.assertNotIn("/health", build()["paths"])
