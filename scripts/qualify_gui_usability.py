"""Exercise keyboard access, native Chromium 200% zoom and mobile reflow.

Run with uv run --locked --with playwright python -m scripts.qualify_gui_usability
--output /workspace/unique-output-directory. Only synthetic inputs are used.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import tempfile
import threading
from pathlib import Path

from pdf_multitool.server import BoundedHTTPServer, Workbench, make_handler
from tests.test_extended import source_fixture


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def layout(page):
    return page.evaluate(
        """() => ({width:innerWidth, scrollWidth:document.documentElement.scrollWidth,
        devicePixelRatio, zoom:getComputedStyle(document.documentElement).zoom,
        smallText:[...document.querySelectorAll('p,span,label,button,summary,input,select,textarea')]
          .filter(e=>e.getClientRects().length && parseFloat(getComputedStyle(e).fontSize)<14)
          .map(e=>({id:e.id,text:e.textContent.slice(0,80),size:getComputedStyle(e).fontSize})),
        unlabelled:[...document.querySelectorAll('input,select,textarea')]
          .filter(e=>e.getClientRects().length && !e.labels.length && !e.getAttribute('aria-label'))
          .map(e=>e.id)})"""
    )


def check_layout(page):
    result = layout(page)
    require(result["scrollWidth"] <= result["width"], f"Horizontal overflow: {result}")
    require(not result["smallText"], f"Small text: {result['smallText']}")
    require(not result["unlabelled"], f"Unlabelled inputs: {result['unlabelled']}")
    return result


def tab_to(page, target, records):
    for _ in range(80):
        page.keyboard.press("Tab")
        focus = page.evaluate(
            """() => {const e=document.activeElement, r=e.getBoundingClientRect(),
            s=getComputedStyle(e);return {id:e.id,tag:e.tagName,
            name:e.getAttribute('aria-label')||e.textContent.slice(0,60),
            visible:e.matches(':focus-visible'),outline:s.outlineStyle,
            outlineWidth:parseFloat(s.outlineWidth),left:r.left,right:r.right,width:innerWidth}}
            """
        )
        if focus["tag"] == "BODY":
            continue
        require(focus["visible"], f"Keyboard focus invisible: {focus}")
        require(focus["outline"] != "none" and focus["outlineWidth"] >= 2, str(focus))
        require(focus["left"] >= 0 and focus["right"] <= focus["width"], str(focus))
        records.append(focus)
        if focus["id"] == target:
            return
    raise RuntimeError(f"Could not reach {target} with Tab")


def native_zoom_screenshot(context, page, path):
    # Playwright's full_page clipping truncates the device-pixel image at native
    # browser zoom on this Chromium. Capture the unmodified visible surface.
    session = context.new_cdp_session(page)
    try:
        result = session.send(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
        )
        path.write_bytes(base64.b64decode(result["data"]))
    finally:
        session.detach()


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    root = parser.parse_args().output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = source_fixture(root / "synthetic-source.pdf")
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    workspace = root / "session"
    workspace.mkdir()
    app = Workbench(workspace)
    server = BoundedHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    report = {"keyboard": [], "layouts": {}, "browser_errors": []}
    try:
        with tempfile.TemporaryDirectory() as temporary, sync_playwright() as playwright:
            # A disposable profile permits the native browser Appearance zoom control.
            context = playwright.chromium.launch_persistent_context(
                Path(temporary) / "profile",
                executable_path=shutil.which("chromium"),
                headless=True,
                no_viewport=True,
                accept_downloads=True,
                reduced_motion="reduce",
                args=[
                    "--no-sandbox",
                    "--window-size=1440,1100",
                ],
            )
            page = context.pages[0]
            page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
            page.goto(f"{base}/#token={app.token}")
            expect(page.locator("#choose-files")).to_be_enabled()
            tab_to(page, "operation", report["keyboard"])
            page.select_option("#operation", "create")
            tab_to(page, "option-content", report["keyboard"])
            page.locator("#option-content").fill(
                "Keyboard-created document\nUnicode: \u03b1 é Ж 中"
            )
            tab_to(page, "run-button", report["keyboard"])
            page.screenshot(path=root / "01-keyboard-run-focus.png", full_page=True)
            page.keyboard.press("Enter")
            expect(page.locator("#cancel-job")).to_be_hidden(timeout=30000)
            expect(page.locator("#run-button")).to_be_enabled()
            require(len(app.jobs) == 1, "Keyboard activation did not submit exactly one job")
            job = next(iter(app.jobs.values()))
            require(job.operation == "create" and job.state == "completed", str(job.snapshot()))
            heading = page.locator("#output-heading").inner_text()
            require("Create" in heading and "results" in heading, heading)
            page.locator("#file-input").set_input_files(source)
            expect(page.locator("#file-count")).to_have_text("1")
            page.select_option("#operation", "ocr_tesseract")
            expect(page.locator("#output-heading")).to_have_text(heading)
            expect(page.locator("#output-context")).to_contain_text("Selected tool: OCR")
            expect(page.locator("#run-button")).to_be_disabled()
            report["layouts"]["desktop"] = check_layout(page)
            page.screenshot(path=root / "02-desktop-ocr-previous-create.png", full_page=True)

            settings = context.new_page()
            settings.goto("chrome://settings/appearance")
            settings.locator("#zoomLevel").select_option(label="200%")
            zoom = float(settings.locator("#zoomLevel").input_value())
            settings.close()
            page.wait_for_function("innerWidth < 1000")
            report["native_browser_zoom"] = zoom
            require(zoom == 2, f"Native tab zoom is {zoom}")
            report["layouts"]["native_zoom_200_percent"] = check_layout(page)
            ratio = report["layouts"]["desktop"]["width"] / layout(page)["width"]
            require(1.95 < ratio < 2.05, f"Zoom did not reflow: ratio={ratio}")
            page.locator("#operation").focus()
            tab_to(page, "option-language", report["keyboard"])
            page.evaluate("scrollTo(0, 0)")
            native_zoom_screenshot(context, page, root / "03-native-200-percent-top.png")
            page.locator("#option-language").scroll_into_view_if_needed()
            native_zoom_screenshot(context, page, root / "03-native-200-percent-controls.png")
            page.locator("#output-panel").scroll_into_view_if_needed()
            native_zoom_screenshot(context, page, root / "03-native-200-percent-output.png")
            page.reload()
            expect(page.locator("#output-heading")).to_have_text(heading)
            expect(page.locator("#results button")).to_have_count(len(job.results))
            require(len(app.jobs) == 1, "Reload resubmitted the operation")
            context.close()

            browser = playwright.chromium.launch(
                executable_path=shutil.which("chromium"), args=["--no-sandbox"]
            )
            for width in (390, 320):
                phone = browser.new_context(
                    viewport={"width": width, "height": 844},
                    is_mobile=True,
                    has_touch=True,
                    reduced_motion="reduce",
                )
                mobile = phone.new_page()
                mobile.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
                mobile.goto(f"{base}/#token={app.token}")
                expect(mobile.locator("#file-count")).to_have_text("1")
                # Restore a completed job without submitting it, as reload recovery does.
                mobile.evaluate(
                    "saved => sessionStorage.setItem('pdf-workbench-job',JSON.stringify(saved))",
                    {"id": job.id, "operation": job.operation},
                )
                mobile.reload()
                expect(mobile.locator("#output-heading")).to_have_text(heading)
                expect(mobile.locator("#operation")).to_be_enabled()
                mobile.select_option("#operation", "ocr_tesseract")
                expect(mobile.locator("#output-context")).to_contain_text("Selected tool: OCR")
                report["layouts"][f"mobile_{width}"] = check_layout(mobile)
                mobile.locator("#operation").focus()
                tab_to(mobile, "option-language", report["keyboard"])
                mobile.screenshot(path=root / f"04-mobile-{width}.png", full_page=True)
                mobile.locator("#theme-toggle").click()
                report["layouts"][f"mobile_{width}_dark"] = check_layout(mobile)
                mobile.screenshot(path=root / f"05-mobile-{width}-dark.png", full_page=True)
                phone.close()
            browser.close()
        require(len(app.jobs) == 1, "Reflow or recovery submitted another job")
        require(not report["browser_errors"], str(report["browser_errors"]))
        require(
            hashlib.sha256(source.read_bytes()).hexdigest() == original_hash, "Original changed"
        )
        report["jobs_created"] = len(app.jobs)
        report["original_sha256"] = original_hash
        report["results_heading"] = heading
        report["passed"] = True
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(root / "report.json"), "passed": True}))
    finally:
        app.close()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
