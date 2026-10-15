"""Opt-in real Chromium QA: uv run --extra html --with playwright pytest tests/test_browser.py.

Uses the installed Chromium executable; every document is synthetic.
"""

import importlib.util
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

import pymupdf

from pdf_multitool.server import BoundedHTTPServer, Workbench, make_handler
from tests.fixtures import document


@unittest.skipUnless(
    importlib.util.find_spec("playwright") and shutil.which("chromium"),
    "Requires playwright and system chromium",
)
class BrowserTests(unittest.TestCase):
    def test_loopback_workflows(self):
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory() as temporary, sync_playwright() as playwright:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            app = Workbench(workspace)
            server = BoundedHTTPServer(("127.0.0.1", 0), make_handler(app))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            browser = playwright.chromium.launch(
                executable_path=shutil.which("chromium"), args=["--no-sandbox"]
            )
            try:
                context = browser.new_context(
                    accept_downloads=True, viewport={"width": 390, "height": 844}
                )
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                base = f"http://127.0.0.1:{server.server_port}"
                page.goto(f"{base}/#token={app.token}")
                source = document(root / "Synthetic résumé & original.pdf")
                original = source.read_bytes()
                page.locator("#file-input").set_input_files(source)
                expect(page.locator("#file-count")).to_have_text("1")
                page.select_option("#operation", "rotate")
                expect(page.locator("#run-button")).to_be_enabled()
                self.assertTrue(
                    page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
                    "Phone layout overflows horizontally",
                )
                # Synchronous repeated clicks must submit only once.
                page.locator("#run-button").evaluate("e => { e.click(); e.click(); e.click(); }")
                expect(page.locator("#results button")).to_have_count(1, timeout=15000)
                self.assertEqual(len(app.jobs), 1)
                with page.expect_download() as download_event:
                    page.locator("#results button").click()
                download = download_event.value
                job = next(iter(app.jobs.values()))
                self.assertEqual(download.suggested_filename, job.results[0]["name"])
                self.assertEqual(
                    Path(download.path()).read_bytes(), Path(job.results[0]["path"]).read_bytes()
                )
                with pymupdf.open(download.path()) as pdf:
                    self.assertEqual(pdf[0].rotation, 90)
                    self.assertIn("Synthetic title", pdf[0].get_text())
                # Originals use a popup viewer. Verify both fetched bytes and HTTP filename.
                page.evaluate("""() => {
                    const originalFetch = window.fetch;
                    window.fetch = async (...args) => {
                        const response = await originalFetch(...args);
                        if (String(args[0]).endsWith('/original')) {
                            window.originalBytes = Array.from(new Uint8Array(
                                await response.clone().arrayBuffer()));
                        }
                        return response;
                    };
                }""")
                with (
                    page.expect_response(lambda r: r.url.endswith("/original")) as response_event,
                    page.expect_popup() as popup_event,
                ):
                    page.get_by_role(
                        "button", name=f"Open original {source.name} in a new tab", exact=True
                    ).click()
                response = response_event.value
                self.assertEqual(response.status, 200)
                page.wait_for_function("window.originalBytes !== undefined")
                self.assertEqual(bytes(page.evaluate("window.originalBytes")), original)
                self.assertIn("filename*=UTF-8''", response.headers["content-disposition"])
                from urllib.parse import unquote

                self.assertIn(source.name, unquote(response.headers["content-disposition"]))
                popup_event.value.close()
                # Each restoration must point to the same job/result and download the same bytes.
                result_name = job.results[0]["name"]
                result_bytes = Path(job.results[0]["path"]).read_bytes()
                for navigation in ("back", "reload"):
                    if navigation == "back":
                        page.goto("about:blank")
                        page.go_back()
                    else:
                        page.reload()
                    restored = page.get_by_role(
                        "button", name=f"Download {result_name}", exact=True
                    )
                    expect(restored).to_be_visible(timeout=15000)
                    self.assertEqual(
                        page.evaluate("JSON.parse(sessionStorage.getItem('pdf-workbench-job')).id"),
                        job.id,
                    )
                    self.assertEqual(len(app.jobs), 1)
                    with (
                        page.expect_download() as restored_event,
                        page.expect_response(
                            lambda r: r.url.endswith(job.results[0]["url"])
                        ) as restored_response,
                    ):
                        restored.click()
                    self.assertEqual(restored_response.value.status, 200)
                    restored_download = restored_event.value
                    self.assertEqual(restored_download.suggested_filename, result_name)
                    self.assertEqual(Path(restored_download.path()).read_bytes(), result_bytes)
                # Transport failure surfaces an error and allows a real retry.
                page.route("**/api/jobs", lambda route: route.abort())
                page.locator("#run-button").click()
                expect(page.locator("#job-status")).to_have_class("job-status is-error")
                expect(page.locator("#run-button")).to_be_enabled()
                page.unroute("**/api/jobs")
                page.locator("#run-button").click()
                expect(page.locator("#results button")).to_have_count(1, timeout=15000)
                self.assertEqual(len(app.jobs), 2)
                # Engine error followed by correction and retry.
                page.locator("#pages").fill("999")
                page.locator("#run-button").click()
                expect(page.locator("#job-errors")).to_contain_text(
                    "page", ignore_case=True, timeout=15000
                )
                expect(page.locator("#run-button")).to_be_enabled()
                page.locator("#pages").fill("1")
                page.locator("#run-button").click()
                expect(page.locator("#results button")).to_have_count(1, timeout=15000)
                # Real renderer cancellation, synchronized on its first generated PNG.
                page.locator("#remove-selected").click()
                expect(page.locator("#file-count")).to_have_text("0")
                many = document(root / "cancel-render.pdf", 100)
                many_bytes = many.read_bytes()
                page.locator("#file-input").set_input_files(many)
                expect(page.locator("#file-count")).to_have_text("1")
                page.select_option("#operation", "render")
                page.locator("#pages").fill("")
                page.select_option("#dpi", "300")
                page.locator("#run-button").click()
                expect(page.locator("#cancel-job")).to_be_visible()
                running = list(app.jobs.values())[-1]
                deadline = time.monotonic() + 15
                while not list(running.directory.rglob("*.png")) and time.monotonic() < deadline:
                    page.wait_for_timeout(10)
                self.assertTrue(
                    list(running.directory.rglob("*.png")), "Renderer never produced its first page"
                )
                self.assertEqual(running.state, "running")
                page.locator("#cancel-job").evaluate("e => { e.click(); e.click(); }")
                expect(page.locator("#run-button")).to_be_enabled(timeout=15000)
                self.assertEqual(running.state, "cancelled")
                self.assertEqual(running.results, [])
                self.assertEqual(many.read_bytes(), many_bytes)
                page.select_option("#operation", "rotate")
                page.locator("#pages").fill("1")
                page.locator("#run-button").click()
                expect(page.locator("#results button")).to_have_count(1, timeout=15000)
                # A completed automatic text download must not repeat when restored.
                page.select_option("#operation", "text")
                page.select_option("#text-export", "download")
                downloads = []
                page.on("download", lambda download: downloads.append(download))
                with page.expect_download() as text_event:
                    page.locator("#run-button").click()
                text_download = text_event.value
                text_bytes = Path(text_download.path()).read_bytes()
                text_job = list(app.jobs.values())[-1]
                expect(page.locator("#run-button")).to_be_enabled()
                page.reload()
                expect(page.locator("#text-result")).to_be_visible(timeout=15000)
                expect(page.locator("#run-button")).to_be_enabled()
                self.assertEqual(page.locator("#extracted-text").input_value(), text_bytes.decode())
                self.assertEqual(len(downloads), 1)
                self.assertEqual(
                    page.evaluate("JSON.parse(sessionStorage.getItem('pdf-workbench-job')).id"),
                    text_job.id,
                )
                # A stale job response clears recovery metadata and permits new work.
                page.route(
                    f"**/api/jobs/{text_job.id}",
                    lambda route: route.fulfill(
                        status=404, content_type="application/json", body='{"error":"Not found"}'
                    ),
                )
                page.reload()
                expect(page.locator("#job-message")).to_contain_text("no longer available")
                self.assertIsNone(page.evaluate("sessionStorage.getItem('pdf-workbench-job')"))
                expect(page.locator("#run-button")).to_be_enabled()
                page.unroute(f"**/api/jobs/{text_job.id}")
                page.select_option("#operation", "rotate")
                page.locator("#pages").fill("1")
                page.locator("#run-button").click()
                expect(page.locator("#results button")).to_have_count(1, timeout=15000)
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(errors, [])
                print(
                    f"Chromium {browser.version}: phone/upload/rotate/download/original/"
                    "back/reload/errors/retry/cancel passed"
                )
            finally:
                browser.close()
                app.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
