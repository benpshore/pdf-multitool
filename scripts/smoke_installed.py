"""Exercise an installed wheel's CLI, static assets and loopback job lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import pymupdf

import pdf_multitool


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def cli(executable, root, *arguments):
    result = subprocess.run(  # noqa: S603 -- installed entry point and explicit argv; no shell
        [str(executable), *map(str, arguments)],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    require(result.returncode == 0, f"CLI failed: {result.stdout[:2000]} {result.stderr[:2000]}")
    return json.loads(result.stdout)


def smoke(html=False):
    installed = Path(pdf_multitool.__file__).resolve()
    require("site-packages" in installed.parts, "Smoke must import the installed wheel")
    executable = Path(sys.executable).parent / "pdf-multitool"
    require(executable.is_file(), "Installed console entry point is absent")
    with tempfile.TemporaryDirectory(prefix="pdf-multitool-installed-smoke-") as temporary:
        root = Path(temporary)
        source = root / "original.pdf"
        with pymupdf.open() as document:
            for page in range(2):
                document.new_page().insert_text((72, 72), f"Installed smoke page {page + 1}")
            document.save(source)
        original = source.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        require(cli(executable, root, "doctor")["engines"]["pymupdf"]["available"], "No PDF engine")
        report = cli(executable, root, "merge", source, source, "-o", root / "merged")
        require(report["state"] == "completed", "Installed merge failed")
        with pymupdf.open(report["results"][0]["path"]) as document:
            require(len(document) == 4, "Merge page count mismatch")
            require("page 2" in document[3].get_text(), "Merge content mismatch")
        if html:
            input_html = root / "input.html"
            input_html.write_text(
                '<h1>Installed HTML smoke</h1><img src="file:///nonexistent-smoke-image.png">'
            )
            report = cli(executable, root, "html", input_html, "-o", root / "html")
            require(report["state"] == "completed", "Installed HTML conversion failed")
            require(
                any("blocked" in warning for warning in report["warnings"]), "Resource gate absent"
            )
            with pymupdf.open(report["results"][0]["path"]) as document:
                require("Installed HTML smoke" in document[0].get_text(), "HTML text missing")
        process = subprocess.Popen(  # noqa: S603 -- installed CLI with fixed argv; no shell
            [str(executable), "gui", "--no-browser"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                require(selector.select(timeout=10), "Loopback server did not start")
                # Keep the ephemeral access token private; never print the startup URL.
                startup = process.stdout.readline().strip()
            require(startup.startswith("PDF Multitool: "), "Unexpected server startup")
            url = urllib.parse.urlsplit(startup.removeprefix("PDF Multitool: "))
            require(url.hostname == "127.0.0.1", "Server must bind loopback")
            token = urllib.parse.parse_qs(url.fragment)["token"][0]

            def request(method, path, body=None, authorized=True, headers=None):
                actual = {"X-PDF-Token": token} if authorized else {}
                actual.update(headers or {})
                if isinstance(body, dict):
                    body = json.dumps(body).encode()
                    actual["Content-Type"] = "application/json"
                connection = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
                try:
                    connection.request(method, path, body, actual)
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()

            for path in ("/", "/app.js", "/app.css"):
                status, body = request("GET", path, authorized=False)
                require(status == 200 and body, f"Installed static asset missing: {path}")
            require(request("GET", "/api/status", authorized=False)[0] == 401, "Auth gate absent")
            require(
                request("GET", "/api/status", headers={"Origin": "https://example.invalid"})[0]
                == 403,
                "Origin gate absent",
            )
            status, body = request(
                "POST", "/api/files", original, headers={"X-Filename": "original.pdf"}
            )
            require(status == 201, "Upload failed")
            file_id = json.loads(body)["id"]
            status, body = request(
                "POST",
                "/api/jobs",
                {"operation": "rotate", "files": [file_id], "options": {"rotation": 90}},
            )
            require(status == 202, "Job submission failed")
            job_id = json.loads(body)["id"]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status, body = request("GET", f"/api/jobs/{job_id}")
                require(status == 200, "Job status unavailable")
                job = json.loads(body)
                if job["state"] not in {"queued", "running"}:
                    break
                time.sleep(0.05)
            require(job["state"] == "completed", "Installed HTTP worker did not complete")
            status, output = request("GET", job["results"][0]["url"])
            require(status == 200, "Result download failed")
            with pymupdf.open(stream=output, filetype="pdf") as document:
                require([page.rotation for page in document] == [90, 90], "Rotation mismatch")
            status, preserved = request("GET", f"/api/files/{file_id}/original")
            require(status == 200 and preserved == original, "Uploaded original changed")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        require(process.returncode == 0, "Server did not shut down cleanly")
        require(hashlib.sha256(source.read_bytes()).hexdigest() == digest, "CLI original changed")
    print(json.dumps({"installed_cli_http_smoke": "passed", "html": html}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", action="store_true", help="Also exercise installed WeasyPrint")
    smoke(parser.parse_args().html)
