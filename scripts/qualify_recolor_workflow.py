"""Qualify whole-document recoloring through the real GUI and independent PDFium.

This checks component conversion and synthetic structure/color invariants. It is
not calibrated color proofing, source-built Poppler comparison or parent acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pypdfium2

import pdf_multitool
from pdf_multitool.server import BoundedHTTPServer, Workbench, make_handler
from tests.test_recolor import recolor_fixture


def require(condition, detail):
    if not condition:
        raise RuntimeError(str(detail))


def raster_and_text(document, index, dpi):
    page = document.get_page(index)
    try:
        textpage = page.get_textpage()
        try:
            text = textpage.get_text_bounded()
        finally:
            textpage.close()
        bitmap = page.render(scale=dpi / 72)
        try:
            return bitmap.to_pil().convert("RGB"), text
        finally:
            bitmap.close()
    finally:
        page.close()


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-installed", action="store_true")
    args = parser.parse_args()
    imported = Path(pdf_multitool.__file__).resolve()
    if args.require_installed:
        require(not imported.is_relative_to(Path(__file__).resolve().parents[1] / "src"), imported)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = recolor_fixture(root / "recolor-source.pdf")
    original = source.read_bytes()
    screenshots, renders, outputs = (root / n for n in ("screenshots", "renders", "outputs"))
    for directory in (screenshots, renders, outputs):
        directory.mkdir()
    cli_output = outputs / "cli"
    cli = subprocess.run(  # noqa: S603 -- fixed installed-module argv, no shell
        [
            sys.executable,
            "-m",
            "pdf_multitool",
            "recolor",
            str(source),
            "--colorspace",
            "rgb",
            "--allow-color-change",
            "-o",
            str(cli_output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    require(cli.returncode == 0, (cli.returncode, cli.stdout, cli.stderr))
    cli_report = json.loads(cli.stdout)
    require(cli_report["state"] == "completed", cli_report)
    cli_pdf = next(cli_output.rglob("*.pdf"))
    session = root / "session"
    session.mkdir()
    app = Workbench(session)
    server = BoundedHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scenarios, errors = [], []
    report = {
        "feature_id": "PY-036",
        "python": platform.python_version(),
        "imported_product": str(imported),
        "installed_required": args.require_installed,
        "versions": {
            p: importlib.metadata.version(p) for p in ("pymupdf", "pypdfium2", "playwright")
        },
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "comparator": "PDFium renders both the original PDF and the MuPDF-converted PDF",
        "parent_review": "not_reviewed",
        "cli": {
            "returncode": cli.returncode,
            "state": cli_report["state"],
            "pdf": str(cli_pdf.relative_to(root)),
            "sha256": hashlib.sha256(cli_pdf.read_bytes()).hexdigest(),
            "comparison": "All PDFium page pixels and text must exactly match the GUI RGB result",
        },
        "tolerances": {
            "gray_channel_spread_max": 2,
            "nonwhite_mask_iou_min": 0.98,
            "rgb_mean_channel_error_max": 1,
            "rgb_fraction_error_above_32_max": 0.01,
            "color_patch_dominant_channel_margin_min": 20,
        },
    }
    try:
        with (
            sync_playwright() as pw,
            pypdfium2.PdfDocument(source) as reference,
            pypdfium2.PdfDocument(cli_pdf) as cli_document,
        ):
            expected = {}
            for index in range(len(reference)):
                for dpi in (150, 300):
                    picture, text = raster_and_text(reference, index, dpi)
                    target = renders / f"source-page-{index + 1}-{dpi}dpi.png"
                    picture.save(target)
                    expected[index, dpi] = (picture, text)
            browser = pw.chromium.launch(
                executable_path=shutil.which("chromium"), args=["--no-sandbox"]
            )
            context = browser.new_context(
                accept_downloads=True, viewport={"width": 1400, "height": 1000}
            )
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/#token={app.token}")
            expect(page.locator("#choose-files")).to_be_enabled()
            page.locator("#file-input").set_input_files(source)
            expect(page.locator("#file-count")).to_have_text("1")
            page.select_option("#operation", "recolor")
            page.locator("#run-button").click()
            expect(page.locator("#run-button")).to_be_enabled(timeout=30000)
            failed = list(app.jobs.values())[-1]
            require(failed.state == "failed" and not failed.results, failed.snapshot())
            for number, mode in enumerate(("gray", "rgb", "cmyk"), 1):
                page.select_option("#option-colorspace", mode)
                page.locator("#option-allow_color_change").check()
                page.locator("#run-button").click()
                expect(page.locator("#run-button")).to_be_enabled(timeout=30000)
                job = list(app.jobs.values())[-1]
                require(job.state == "completed" and job.operation == "recolor", job.snapshot())
                require(job.options["colorspace"] == mode, job.options)
                expect(page.locator("#output-heading")).to_have_text("Convert PDF colors results")
                directory = outputs / mode
                directory.mkdir()
                for item in job.results:
                    with page.expect_download() as event:
                        page.get_by_role(
                            "button", name=f"Download {item['name']}", exact=True
                        ).click()
                    content = Path(event.value.path()).read_bytes()
                    require(content == Path(item["path"]).read_bytes(), "Download mismatch")
                    (directory / item["name"]).write_bytes(content)
                output = next(directory.glob("*.pdf"))
                scenario = {
                    "mode": mode,
                    "snapshot": job.snapshot(),
                    "pdf": str(output.relative_to(root)),
                    "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                    "pages": [],
                }
                with pypdfium2.PdfDocument(output) as converted:
                    require(len(converted) == len(reference), "Page count changed")
                    for index in range(len(converted)):
                        checks = []
                        for dpi in (150, 300):
                            picture, text = raster_and_text(converted, index, dpi)
                            expected_picture, expected_text = expected[index, dpi]
                            require(text == expected_text, "Independent text extraction changed")
                            require(
                                picture.size == expected_picture.size, "Page dimensions changed"
                            )
                            target = renders / f"{mode}-page-{index + 1}-{dpi}dpi.png"
                            picture.save(target)
                            a, b = np.asarray(picture), np.asarray(expected_picture)
                            if mode == "rgb":
                                cli_picture, cli_text = raster_and_text(cli_document, index, dpi)
                                require(cli_text == text, "CLI and GUI text differs")
                                require(
                                    np.array_equal(a, np.asarray(cli_picture)),
                                    "CLI and GUI page pixels differ",
                                )
                            mask_a, mask_b = a.min(axis=2) < 245, b.min(axis=2) < 245
                            union = int(np.logical_or(mask_a, mask_b).sum())
                            iou = (
                                float(np.logical_and(mask_a, mask_b).sum() / union)
                                if union
                                else 1.0
                            )
                            require(iou >= 0.98, (mode, index, dpi, iou))
                            spread = int((a.max(axis=2).astype(int) - a.min(axis=2)).max())
                            delta = np.abs(a.astype(np.int16) - b.astype(np.int16))
                            mean_error = float(delta.mean())
                            large = float((delta.max(axis=2) > 32).mean())
                            if mode == "gray":
                                require(spread <= 2, (mode, index, dpi, spread))
                            if mode == "rgb":
                                require(
                                    mean_error <= 1 and large <= 0.01,
                                    (mode, index, dpi, mean_error, large),
                                )
                            if index == 2:
                                require(bool(np.all(a == 255)), "Blank page is not blank")
                            elif mode != "gray":
                                positions = (
                                    [(60, 55), (175, 50), (180, 125)]
                                    if index == 0
                                    else [(105, 50), (110, 165), (35, 170)]
                                )
                                for (x, y), dominant in zip(positions, (0, 2, 1), strict=True):
                                    pixel = a[round(y * dpi / 72), round(x * dpi / 72)].astype(int)
                                    require(
                                        pixel[dominant]
                                        - max(pixel[i] for i in range(3) if i != dominant)
                                        >= 20,
                                        (mode, index, pixel),
                                    )
                            checks.append(
                                {
                                    "dpi": dpi,
                                    "dimensions": list(picture.size),
                                    "render": str(target.relative_to(root)),
                                    "nonwhite_mask_iou": iou,
                                    "max_channel_spread": spread,
                                    "mean_channel_difference_from_source": mean_error,
                                    "fraction_difference_above_32": large,
                                    "independent_text_matches": True,
                                }
                            )
                        scenario["pages"].append({"page": index + 1, "checks": checks})
                scenarios.append(scenario)
                page.screenshot(
                    path=screenshots / f"{number:02d}-{mode}-desktop.png", full_page=True
                )
            page.reload()
            expect(page.locator("#output-heading")).to_have_text("Convert PDF colors results")
            expect(page.locator("#results button")).to_have_count(2)
            require(len(app.jobs) == 4, "Reload resubmitted conversion")
            page.set_viewport_size({"width": 390, "height": 844})
            require(
                page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
                "Mobile overflow",
            )
            page.screenshot(path=screenshots / "04-recolor-mobile.png", full_page=True)
            browser.close()
        require(source.read_bytes() == original, "Original changed")
        require(not errors, errors)
        report.update(
            passed=True,
            scenarios=scenarios,
            acknowledgment_failure_tested=True,
            browser_errors=errors,
        )
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {"report": str(root / "report.json"), "passed": True, "scenarios": len(scenarios)}
            )
        )
    finally:
        app.close()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
