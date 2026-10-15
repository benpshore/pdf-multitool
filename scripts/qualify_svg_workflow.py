"""Qualify PY-006 through the real browser and optional installed distribution.

SVG images are decoded by Chromium with all network requests blocked. PDFium is
the independent PDF raster comparator. This does not substitute for the required
source-built Poppler comparator or parent visual acceptance.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import json
import math
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pypdfium2
from PIL import Image

import pdf_multitool
from pdf_multitool.server import BoundedHTTPServer, Workbench, make_handler
from tests.test_svg import svg_fixture

# Frozen synthetic tolerances: exact interior color patches and localized text
# ink plus bounded whole-image differences between different rasterizers.
MAX_MEAN_CHANNEL_ERROR = 3.0
MAX_LARGE_DIFFERENCE_FRACTION = 0.02


def require(condition, detail):
    if not condition:
        raise RuntimeError(str(detail))


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
    source = svg_fixture(root / "svg-source.pdf")
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
            "svg",
            str(source),
            "--pages",
            "2",
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
    cli_svg = next(cli_output.rglob("*.svg"))
    session = root / "session"
    session.mkdir()
    app = Workbench(session)
    server = BoundedHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors, scenarios, requests = [], [], []
    report = {
        "feature_id": "PY-006",
        "python": platform.python_version(),
        "imported_product": str(imported),
        "installed_required": args.require_installed,
        "versions": {
            p: importlib.metadata.version(p) for p in ("pymupdf", "pypdfium2", "playwright")
        },
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "comparison": "Chromium SVG image versus PDFium PDF; not source-built Poppler",
        "tolerances": {
            "mean_channel_error_max": MAX_MEAN_CHANNEL_ERROR,
            "large_difference_fraction_max": MAX_LARGE_DIFFERENCE_FRACTION,
            "large_difference_threshold": 64,
            "paths_text_ink_ratio": [0.8, 1.25],
        },
        "parent_review": "not_reviewed",
        "cli": {
            "returncode": cli.returncode,
            "state": cli_report["state"],
            "stderr": cli.stderr,
            "svg": str(cli_svg.relative_to(root)),
            "sha256": hashlib.sha256(cli_svg.read_bytes()).hexdigest(),
            "render_equivalence": "Must exactly match paths page 2 from the GUI",
        },
    }
    try:
        with sync_playwright() as pw, pypdfium2.PdfDocument(source) as reference:
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
            raster_context = browser.new_context(device_scale_factor=1)
            raster_context.route(
                "**/*", lambda route: (requests.append(route.request.url), route.abort())
            )
            raster = raster_context.new_page()
            for number, (mode, scale) in enumerate((("paths", 1), ("text", 1.5)), 1):
                page.select_option("#operation", "svg")
                page.select_option("#option-text_mode", mode)
                page.locator("#option-scale").fill(str(scale))
                page.locator("#pages").fill("1-3")
                page.locator("#run-button").click()
                expect(page.locator("#run-button")).to_be_enabled(timeout=30000)
                expect(page.locator("#cancel-job")).to_be_hidden()
                job = list(app.jobs.values())[-1]
                require(job.state == "completed" and job.operation == "svg", job.snapshot())
                require(
                    job.options["scale"] == scale and job.options["text_mode"] == mode, job.options
                )
                expect(page.locator("#output-heading")).to_have_text("Export SVG pages results")
                jobdir = outputs / mode
                jobdir.mkdir()
                for result in job.results:
                    with page.expect_download() as event:
                        page.get_by_role(
                            "button", name=f"Download {result['name']}", exact=True
                        ).click()
                    content = Path(event.value.path()).read_bytes()
                    require(content == Path(result["path"]).read_bytes(), "Download mismatch")
                    (jobdir / result["name"]).write_bytes(content)
                manifest_path = next(jobdir.glob("*.json"))
                manifest = json.loads(manifest_path.read_text())
                scenario = {"mode": mode, "scale": scale, "snapshot": job.snapshot(), "pages": []}
                for entry in manifest["pages"]:
                    index = entry["source_page"] - 1
                    svg = (jobdir / entry["file"]).read_bytes()
                    if mode == "paths" and index == 1:
                        require(svg == cli_svg.read_bytes(), "CLI and GUI SVG bytes differ")
                    checks = []
                    for dpi in (150, 300):
                        factor = dpi / 72
                        pdf_page = reference.get_page(index)
                        try:
                            bitmap = pdf_page.render(scale=scale * factor)
                            expected = bitmap.to_pil().convert("RGB")
                            bitmap.close()
                        finally:
                            pdf_page.close()
                        width, height = expected.size
                        raster.set_viewport_size({"width": width, "height": height})
                        encoded = base64.b64encode(svg).decode("ascii")
                        raster.set_content(
                            '<html><body style="margin:0;background:white">'
                            f'<img width="{width}" height="{height}" '
                            f'src="data:image/svg+xml;base64,{encoded}">'
                            "</body></html>"
                        )
                        raster.locator("img").evaluate("image => image.decode()")
                        target = renders / f"{mode}-page-{index + 1}-{dpi}dpi-svg.png"
                        raster.screenshot(path=target)
                        actual = Image.open(target).convert("RGB")
                        expected_path = renders / f"{mode}-page-{index + 1}-{dpi}dpi-pdfium.png"
                        expected.save(expected_path)
                        require(actual.size == expected.size, (actual.size, expected.size))
                        a, b = np.asarray(actual), np.asarray(expected)
                        delta = np.abs(a.astype(np.int16) - b.astype(np.int16))
                        mean_error = float(delta.mean())
                        large = float((delta.max(axis=2) > 64).mean())
                        if index < 2:
                            positions = (
                                [(60, 55), (175, 50), (180, 125)]
                                if index == 0
                                else [(105, 50), (110, 165), (35, 170)]
                            )
                            for (x, y), color in zip(
                                positions, [(255, 0, 0), (0, 0, 255), (0, 180, 0)], strict=True
                            ):
                                pixel = a[
                                    math.floor(y * scale * factor), math.floor(x * scale * factor)
                                ]
                                require(
                                    np.max(np.abs(pixel.astype(int) - color)) <= 1,
                                    (mode, index, pixel, color),
                                )
                            box = (20, 85, 95, 106) if index == 0 else (54, 10, 75, 85)
                            x0, y0, x1, y1 = [round(v * scale * factor) for v in box]
                            a_ink = int((a[y0:y1, x0:x1].max(axis=2) < 180).sum())
                            b_ink = int((b[y0:y1, x0:x1].max(axis=2) < 180).sum())
                            require(b_ink > 0, "Independent reference glyph region is empty")
                            ink_ratio = a_ink / b_ink
                            if mode == "paths":
                                require(0.8 <= ink_ratio <= 1.25, (index, dpi, ink_ratio))
                        else:
                            require(bool(np.all(a == 255)), "Declared blank SVG rendered nonblank")
                            ink_ratio = None
                        if mode == "paths":
                            require(
                                mean_error <= MAX_MEAN_CHANNEL_ERROR
                                and large <= MAX_LARGE_DIFFERENCE_FRACTION,
                                (index, dpi, mean_error, large),
                            )
                        checks.append(
                            {
                                "dpi": dpi,
                                "pixel_dimensions": [width, height],
                                "mean_channel_error": mean_error,
                                "large_difference_fraction": large,
                                "unicode_region_ink_ratio": ink_ratio,
                                "svg_raster": str(target.relative_to(root)),
                                "pdf_raster": str(expected_path.relative_to(root)),
                                "font_fidelity_asserted": mode == "paths",
                            }
                        )
                    scenario["pages"].append(
                        {
                            "source_page": index + 1,
                            "svg": str((jobdir / entry["file"]).relative_to(root)),
                            "checks": checks,
                        }
                    )
                scenarios.append(scenario)
                page.screenshot(
                    path=screenshots / f"{number:02d}-{mode}-desktop.png", full_page=True
                )
            page.reload()
            expect(page.locator("#output-heading")).to_have_text("Export SVG pages results")
            expect(page.locator("#results button")).to_have_count(4)
            require(len(app.jobs) == 2, "Reload resubmitted SVG export")
            page.set_viewport_size({"width": 390, "height": 844})
            require(
                page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
                "Mobile overflow",
            )
            page.screenshot(path=screenshots / "03-svg-mobile.png", full_page=True)
            browser.close()
        require(source.read_bytes() == original, "Original changed")
        require(
            not requests and not errors, {"external_requests": requests, "browser_errors": errors}
        )
        report.update(
            passed=True, scenarios=scenarios, browser_errors=errors, svg_external_requests=requests
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
