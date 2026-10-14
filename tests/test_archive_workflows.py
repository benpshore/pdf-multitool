"""Real HTTP, CLI and native-PDF archive/structured-export integration checks."""
import gzip
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile

import pymupdf
from pdf_multitool.server import Workbench, BoundedHTTPServer, make_handler
from tests.fixtures import document, scanned


class ArchiveWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Workbench(self.root)
        self.server = BoundedHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.pdf = document(self.root / "fixture.pdf", 2).read_bytes()

    def tearDown(self):
        self.app.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        actual = {"X-PDF-Token": self.app.token, **(headers or {})}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            actual["Content-Type"] = "application/json"
        conn.request(method, path, body, actual)
        response = conn.getresponse()
        status, payload = response.status, response.read()
        conn.close()
        return status, payload

    def upload(self, name, data):
        status, body = self.request("POST", "/api/files", data, {"X-Filename": name})
        self.assertEqual(status, 201, body)
        return json.loads(body)

    def zip(self, members):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in members:
                archive.writestr(name, data)
        return output.getvalue()

    def wait_job(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, body = self.request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(status, 200, body)
            job = json.loads(body)
            if job["state"] not in {"queued", "running"}:
                return job
            time.sleep(0.05)
        self.fail("Processing did not finish in test deadline")

    def process(self, operation, ids, options=None):
        status, body = self.request("POST", "/api/jobs", {"operation": operation, "files": ids, "options": options or {}})
        self.assertEqual(status, 202, body)
        return self.wait_job(json.loads(body)["id"])

    def test_zip_upload_expand_process_and_keep_original(self):
        data = self.zip([("folder/one.pdf", self.pdf), ("two.pdf", self.pdf), ("readme.txt", b"not a supported document")])
        imported = self.upload("batch.zip", data)
        self.assertEqual(len(imported["files"]), 2)
        self.assertTrue(imported["warnings"])
        self.assertEqual(imported["files"][0]["member_path"], "folder/one.pdf")
        status, original = self.request("GET", imported["files"][0]["source_archive"]["url"])
        self.assertEqual((status, original), (200, data))
        job = self.process("merge", [f["id"] for f in imported["files"]])
        self.assertEqual(job["state"], "completed", job)
        _, result = self.request("GET", job["results"][0]["url"])
        with pymupdf.open(stream=result, filetype="pdf") as doc:
            self.assertEqual(len(doc), 4)
        self.assertEqual(self.app.pending_uploads, {})
        self.assertEqual(list((self.root / "imports").iterdir()), [])

    def test_unsafe_archive_is_atomic_and_leaves_no_reservation(self):
        data = self.zip([("good.pdf", self.pdf), ("../escape.pdf", self.pdf)])
        status, _ = self.request("POST", "/api/files", data, {"X-Filename": "evil.zip"})
        self.assertEqual(status, 400)
        self.assertEqual(self.app.files, {})
        self.assertEqual(self.app.archives, {})
        self.assertEqual(self.app.pending_uploads, {})
        self.assertEqual(list((self.root / "inputs").iterdir()), [])
        self.assertEqual(list((self.root / "imports").iterdir()), [])

    def test_long_archive_member_name_retains_pdf_type(self):
        imported = self.upload("batch.zip", self.zip([("a" * 210 + ".pdf", self.pdf)]))
        file = imported["files"][0]
        self.assertTrue(file["name"].endswith(".pdf"))
        self.assertLessEqual(len(file["name"].encode()), 180)
        self.assertTrue(self.app.files[file["id"]]["path"].endswith(".pdf"))
        job = self.process("textlayout", [file["id"]])
        self.assertEqual(job["state"], "completed", job)

    def test_nested_archives_not_expanded_and_sources_deleted_with_last_member(self):
        nested = self.zip([("hidden.pdf", self.pdf)])
        imported = self.upload("batch.zip", self.zip([("visible.pdf", self.pdf), ("nested.zip", nested)]))
        self.assertEqual(len(imported["files"]), 1)
        self.assertTrue(any("nested" in w.lower() for w in imported["warnings"]))
        file = imported["files"][0]
        self.assertEqual(self.request("DELETE", f'/api/files/{file["id"]}')[0], 200)
        self.assertEqual(self.app.archives, {})
        self.assertEqual(self.request("GET", file["source_archive"]["url"])[0], 404)

    def test_gzip_upload_and_tar_bundle_integrity(self):
        imported = self.upload("document.pdf.gz", gzip.compress(self.pdf))
        job = self.process("textlayout", [imported["files"][0]["id"]])
        self.assertEqual(job["state"], "completed", job)
        status, body = self.request("POST", "/api/bundles", {"job": job["id"], "format": "tar.gz"})
        self.assertEqual(status, 202, body)
        bundle = self.wait_job(json.loads(body)["id"])
        self.assertEqual(bundle["state"], "completed", bundle)
        _, data = self.request("GET", bundle["results"][0]["url"])
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            names = archive.getnames()
            self.assertEqual(names, ["document-0001/document.txt"])
            self.assertIn(b"visible test content", archive.extractfile(names[0]).read())

    def test_markdown_zip_preserves_image_links_and_does_not_bundle_inputs(self):
        image_pdf = scanned(self.root / "scan.pdf").read_bytes()
        uploaded = self.upload("scan.pdf", image_pdf)
        job = self.process("markdown", [uploaded["id"]])
        self.assertEqual(job["state"], "completed", job)
        self.assertGreaterEqual(len(job["results"]), 2)
        status, body = self.request("POST", "/api/bundles", {"job": job["id"], "format": "zip"})
        self.assertEqual(status, 202, body)
        bundle = self.wait_job(json.loads(body)["id"])
        self.assertEqual(bundle["state"], "completed", bundle)
        _, data = self.request("GET", bundle["results"][0]["url"])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            md_path = next(n for n in names if n.endswith(".md"))
            md = archive.read(md_path).decode()
            import re
            links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", md)
            self.assertTrue(links)
            for link in links:
                self.assertIn((Path(md_path).parent / link).as_posix(), names)
            self.assertFalse(any(n.endswith(".pdf") for n in names))
            self.assertFalse(any(n.endswith("worker.log") or n.endswith("result.json") for n in names))

    def test_cli_archive_input_runs_native_operation(self):
        path = self.root / "cli-batch.zip"
        path.write_bytes(self.zip([("a.pdf", self.pdf), ("b.pdf", self.pdf)]))
        result = subprocess.run([sys.executable, "-m", "pdf_multitool", "merge", str(path), "-o", str(self.root / "cli-output")], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["state"], "completed")
        with pymupdf.open(report["results"][0]["path"]) as doc:
            self.assertEqual(len(doc), 4)


if __name__ == "__main__":
    unittest.main()
