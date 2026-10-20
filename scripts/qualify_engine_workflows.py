"""Run real browser workflows and retain every synthetic input, result and render.

uv run --locked --extra html --extra ocr --with playwright python \
    -m scripts.qualify_engine_workflows --output /workspace/unique-inspection-directory
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import threading
from pathlib import Path

import pymupdf

from pdf_multitool.core import engines
from pdf_multitool.server import BoundedHTTPServer, Workbench, make_handler
from tests.test_extended import scan_fixture, source_fixture


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    fixtures = root / "fixtures"
    fixtures.mkdir()
    source = source_fixture(fixtures / "tables-and-attachments.pdf")
    scan = scan_fixture(fixtures / "scanned-invoice.pdf")
    skew = scan_fixture(fixtures / "skewed-invoice.pdf", 4)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in fixtures.iterdir()}
    workspace = root / "session"
    workspace.mkdir()
    screenshots = root / "screenshots"
    screenshots.mkdir()
    app = Workbench(workspace)
    server = BoundedHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    observations, errors = [], []
    scenarios = [
        ("create", None, {"content": "Created in the browser\nUnicode: \u03b1 é Ж 中\n" * 8}),
        ("paginate", source, {"header": "Inspection copy", "footer": "Page {page} of {pages}"}),
        ("tables", source, {}),
        ("geometry", source, {"dedupe_chars": True}),
        ("optimize", source, {"compression_level": 9}),
        ("inspectpdf", source, {}),
        ("attachments", source, {}),
        ("deduplicate", source, {}),
        ("render_native", source, {"renderer": "pymupdf"}),
        ("render_native", source, {"renderer": "ghostscript"}),
        ("ocr_tesseract", scan, {"allow_rasterize": True}),
        ("deskew", skew, {"allow_rasterize": True}),
    ]
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=shutil.which("chromium"), args=["--no-sandbox"]
            )
            context = browser.new_context(
                accept_downloads=True, viewport={"width": 1440, "height": 1100}
            )
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            base = f"http://127.0.0.1:{server.server_port}"
            page.goto(f"{base}/#token={app.token}")
            expect(page.locator("#choose-files")).to_be_enabled()
            page.screenshot(path=screenshots / "01-desktop-empty.png", full_page=True)
            previous = None
            for index, (operation, source_path, options) in enumerate(scenarios, 2):
                if source_path != previous:
                    if previous:
                        page.locator("#remove-selected").click()
                        expect(page.locator("#file-count")).to_have_text("0")
                    if source_path:
                        page.locator("#file-input").set_input_files(source_path)
                        expect(page.locator("#file-count")).to_have_text("1")
                previous = source_path
                page.select_option("#operation", operation)
                for key, value in options.items():
                    control = page.locator(f"#option-{key}")
                    if isinstance(value, bool):
                        control.set_checked(value)
                    elif control.evaluate("e => e.tagName") == "SELECT":
                        control.select_option(str(value))
                    else:
                        control.fill(str(value))
                expect(page.locator("#run-button")).to_be_enabled()
                page.locator("#run-button").click()
                expect(page.locator("#cancel-job")).to_be_hidden(timeout=30000)
                expect(page.locator("#run-button")).to_be_enabled()
                job = list(app.jobs.values())[-1]
                require(job.state == "completed", str(job.snapshot()))
                require(job.operation == operation, "GUI submitted the wrong operation")
                with page.expect_download() as event:
                    page.locator("#results button").first.click()
                downloaded = event.value
                require(
                    Path(downloaded.path()).read_bytes()
                    == Path(job.results[0]["path"]).read_bytes(),
                    "Download differs from published result",
                )
                page.screenshot(path=screenshots / f"{index:02d}-{operation}.png", full_page=True)
                observations.append(
                    {
                        "operation": operation,
                        "options": options,
                        "snapshot": job.snapshot(),
                        "results": [
                            {"file": str(Path(r["path"]).relative_to(root)), "name": r["name"]}
                            for r in job.results
                        ],
                    }
                )
            # Real reload recovery, keyboard focus and phone layout for the new controls.
            last_id = list(app.jobs)[-1]
            page.reload()
            expect(page.locator("#cancel-job")).to_be_hidden()
            expect(page.locator("#results button")).to_have_count(
                len(list(app.jobs.values())[-1].results)
            )
            # Rasterization acknowledgment intentionally resets; recovery is not resubmission.
            expect(page.locator("#run-button")).to_be_disabled()
            require(len(app.jobs) == len(scenarios), "Reload resubmitted a job")
            require(
                page.evaluate("JSON.parse(sessionStorage.getItem('pdf-workbench-job')).id")
                == last_id,
                "Lost job recovery",
            )
            page.set_viewport_size({"width": 390, "height": 844})
            page.select_option("#operation", "ocr_tesseract")
            require(
                page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
                "Phone layout overflows",
            )
            page.locator("#option-language").focus()
            require(
                page.locator("#option-language").evaluate("e => e.labels.length === 1"),
                "Missing accessible label",
            )
            page.screenshot(path=screenshots / "14-phone-ocr.png", full_page=True)
            browser.close()
    finally:
        app.close()
        server.shutdown()
        server.server_close()
        thread.join()
    require(not errors, f"Browser errors: {errors}")
    require(
        before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in fixtures.iterdir()},
        "Original bytes changed",
    )
    renders = root / "renders"
    renders.mkdir()
    inventory = []
    pdfs = list(fixtures.glob("*.pdf")) + [
        Path(r["path"]) for j in app.jobs.values() for r in j.results if r["path"].endswith(".pdf")
    ]
    for number, path in enumerate(pdfs, 1):
        with pymupdf.open(path) as pdf:
            for index, page in enumerate(pdf):
                images = []
                for dpi in (72, 144):
                    target = renders / f"document-{number:02d}-page-{index + 1:03d}-{dpi}dpi.png"
                    page.get_pixmap(dpi=dpi, alpha=False).save(target)
                    images.append(str(target.relative_to(root)))
                inventory.append(
                    {
                        "document": str(path.relative_to(root)),
                        "page": index + 1,
                        "dimensions": list(page.rect),
                        "renders": images,
                        "text": page.get_text(),
                    }
                )
    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "engines": engines(),
        "fixture_sha256": before,
        "browser_errors": errors,
        "scenarios": observations,
        "page_inventory": inventory,
        "inspection": "Generated for independent visual review; not a claim of human acceptance.",
    }
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    files = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }
    (root / "sha256.json").write_text(json.dumps(files, indent=2) + "\n")
    print(
        json.dumps(
            {
                "report": str(root / "report.json"),
                "scenarios": len(observations),
                "pages": len(inventory),
                "screenshots": len(list(screenshots.glob("*.png"))),
            }
        )
    )


if __name__ == "__main__":
    main()
