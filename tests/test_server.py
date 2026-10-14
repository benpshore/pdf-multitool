import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.parse

from pdf_multitool.server import Workbench, make_handler, BoundedHTTPServer
from tests.fixtures import document


class RealHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Workbench(self.root)
        self.server = BoundedHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.pdf = document(self.root / "synthetic.pdf").read_bytes()

    def tearDown(self):
        self.app.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, auth=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        actual = {"X-PDF-Token": self.app.token} if auth else {}
        actual.update(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            actual["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=actual)
        response = conn.getresponse()
        payload = response.read()
        status, response_headers = response.status, dict(response.getheaders())
        conn.close()
        return status, response_headers, payload

    def upload(self):
        status, _, body = self.request("POST", "/api/files", self.pdf,
                                       headers={"X-Filename": urllib.parse.quote('evil <script>".pdf')})
        self.assertEqual(status, 201, body)
        return json.loads(body)

    def test_token_and_origin_boundaries(self):
        self.assertEqual(self.request("GET", "/api/status", auth=False)[0], 401)
        self.assertEqual(self.request("GET", "/api/status", headers={"Origin": "https://attacker.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/api/status", headers={"Host": "attacker.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/api/status")[0], 200)

    def test_full_upload_process_download_preserves_original(self):
        item = self.upload()
        _, _, body = self.request("POST", "/api/jobs", {"operation": "rotate", "files": [item["id"]], "options": {"rotation": 90}})
        job_id = json.loads(body)["id"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            _, _, body = self.request("GET", f"/api/jobs/{job_id}")
            job = json.loads(body)
            if job["state"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "completed", job)
        status, headers, output = self.request("GET", job["results"][0]["url"])
        self.assertEqual(status, 200)
        self.assertTrue(output.startswith(b"%PDF-"))
        self.assertIn("attachment;", headers["Content-Disposition"])
        _, _, original = self.request("GET", f'/api/files/{item["id"]}/original')
        self.assertEqual(original, self.pdf)
        self.assertNotIn("path", job["results"][0])

    def test_delete_and_unknown_routes(self):
        item = self.upload()
        self.assertEqual(self.request("DELETE", f'/api/files/{item["id"]}')[0], 200)
        self.assertEqual(self.request("GET", f'/api/files/{item["id"]}/original')[0], 404)
        self.assertEqual(self.request("GET", "/../../etc/passwd")[0], 404)

    def test_invalid_jobs_and_file_types(self):
        self.assertEqual(self.request("POST", "/api/files", b"no", headers={"X-Filename": "bad.exe"})[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", {"operation": "rotate", "files": []})[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", b"not JSON")[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", b"[]")[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", {"operation": "rotate", "files": [{}]})[0], 400)

    def test_upload_is_private_and_reservation_released(self):
        item = self.upload()
        file = Path(self.app.files[item["id"]]["path"])
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.app.pending_uploads, {})

    def test_static_assets_resolve_from_actual_html(self):
        status, _, html = self.request("GET", "/", auth=False)
        self.assertEqual(status, 200)
        import re
        for path in re.findall(rb'(?:href|src)="(/static/[^\"]+)"', html):
            status, headers, content = self.request("GET", path.decode(), auth=False)
            self.assertEqual(status, 200, path)
            self.assertGreater(len(content), 100)


if __name__ == "__main__":
    unittest.main()
