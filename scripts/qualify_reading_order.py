"""Capture actual GUI selection, originals, outputs and source/output renders.

Explicit-policy results and the unchanged automatic-order failure are separate.
Ada's visual acceptance remains pending; no automatic semantic repair is claimed.
"""

import argparse
import hashlib
import html
import json
import platform
import shutil
import threading
from pathlib import Path

import pypdfium2

from experiments.table_prose_current_v1.accept import export_body
from pdf_multitool.server import BoundedHTTPServer, Workbench, make_handler

REPO = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise RuntimeError(str(message))


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    root = parser.parse_args().output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source_dir = root / "originals"
    source_dir.mkdir()
    originals = REPO / "experiments/representation_v1"
    fresh = REPO / "experiments/row_order_contract_v1"
    old_expected = json.loads((originals / "expectations.json").read_text())["fixtures"]
    new_expected = json.loads((fresh / "contracts.json").read_text())
    cases = [
        (originals, "table_row_operators", "rows", old_expected["table_row_operators"]),
        (originals, "table_column_operators", "rows", old_expected["table_column_operators"]),
        (fresh, "numeric_cells_reverse", "rows", new_expected["numeric_cells_reverse"]["row_text"]),
        (
            fresh,
            "short_column_prose_rows",
            "columns",
            new_expected["short_column_prose_rows"]["column_text"],
        ),
        (fresh, "ambiguous_grid_rows", "rows", new_expected["ambiguous_grid_rows"]["row_text"]),
        (
            fresh,
            "ambiguous_grid_rows",
            "columns",
            new_expected["ambiguous_grid_rows"]["column_text"],
        ),
        (originals, "table_column_operators", "auto", "A1 A2 A3 B1 B2 B3"),
    ]
    fixture_hashes = {}
    for corpus, name, _, _ in cases:
        target = source_dir / (name + ".pdf")
        if not target.exists():
            shutil.copy2(corpus / "fixtures" / target.name, target)
            fixture_hashes[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
            with pypdfium2.PdfDocument(target) as doc:
                page = doc.get_page(0)
                for dpi in (72, 144, 288):
                    bitmap = page.render(scale=dpi / 72)
                    bitmap.to_pil().save(source_dir / f"{name}-{dpi}dpi.png")
                    bitmap.close()
                page.close()
    session = root / "session"
    session.mkdir()
    app = Workbench(session)
    server = BoundedHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    reports, errors = [], []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path=shutil.which("chromium"), args=["--no-sandbox"]
            )
            context = browser.new_context(
                accept_downloads=True, viewport={"width": 1280, "height": 900}
            )
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/#token={app.token}")
            expect(page.locator("#choose-files")).to_be_enabled()
            rendering = browser.new_context(viewport={"width": 960, "height": 800})
            rendering.route("**/*", lambda route: route.abort())
            view = rendering.new_page()
            previous = None
            for _, name, mode, expected in cases:
                if previous != name:
                    if previous:
                        page.locator("#remove-selected").click()
                        expect(page.locator("#file-count")).to_have_text("0")
                    page.locator("#file-input").set_input_files(source_dir / (name + ".pdf"))
                    expect(page.locator("#file-count")).to_have_text("1")
                    previous = name
                for operation, suffix in (
                    ("textlayout", "txt"),
                    ("markdown", "md"),
                    ("pdfhtml", "html"),
                ):
                    page.select_option("#operation", "text")
                    page.select_option("#text-export", operation)
                    expect(page.locator("#reading-order-field")).to_be_visible()
                    page.select_option("#reading-order", mode)
                    page.locator("#run-button").click()
                    expect(page.locator("#cancel-job")).to_be_hidden(timeout=30000)
                    expect(page.locator("#run-button")).to_be_enabled()
                    job = list(app.jobs.values())[-1]
                    require(job.state == "completed", job.snapshot())
                    require(
                        job.options["reading_order"] == mode,
                        "GUI did not submit the selected policy",
                    )
                    out = root / "outputs" / (name + "-" + mode) / suffix
                    out.mkdir(parents=True)
                    for result in job.results:
                        with page.expect_download() as event:
                            page.get_by_role(
                                "button", name="Download " + result["name"], exact=True
                            ).click()
                        data = Path(event.value.path()).read_bytes()
                        require(
                            data == Path(result["path"]).read_bytes(),
                            "Published/downloaded bytes differ",
                        )
                        target = out / (
                            "export." + suffix
                            if result["name"].endswith("." + suffix)
                            else result["name"]
                        )
                        target.write_bytes(data)
                    observed = export_body(out, suffix)
                    require(
                        observed == expected,
                        {
                            "case": name,
                            "mode": mode,
                            "format": suffix,
                            "expected": expected,
                            "observed": observed,
                        },
                    )
                    content = (out / ("export." + suffix)).read_text()
                    if suffix != "html":
                        content = (
                            '<!doctype html><meta charset="utf-8"><style>'
                            "body{font:18px monospace;padding:24px}pre{white-space:pre-wrap}"
                            "</style><pre>" + html.escape(content) + "</pre>"
                        )
                    view.set_content(content)
                    view.screenshot(path=out / "render.png", full_page=True)
                    reports.append(
                        {
                            "case": name,
                            "mode": mode,
                            "operation": operation,
                            "expected": expected,
                            "observed": observed,
                            "matches_selected_policy": True,
                            "matches_original_table_oracle": observed == old_expected.get(name)
                            if name in old_expected
                            else None,
                            "source": "originals/" + name + ".pdf",
                            "source_render": "originals/" + name + "-144dpi.png",
                            "output": str((out / ("export." + suffix)).relative_to(root)),
                            "output_render": str((out / "render.png").relative_to(root)),
                            "warnings": job.warnings,
                            "semantics_verified": False,
                            "ada_review": "not_reviewed",
                        }
                    )
            page.reload()
            expect(page.locator("#results button")).to_have_count(2)
            require(len(app.jobs) == 21, "Reload must not submit a new job")
            page.set_viewport_size({"width": 390, "height": 844})
            page.select_option("#operation", "textlayout")
            expect(page.locator("#reading-order-field")).to_be_visible()
            require(
                page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
                "Mobile layout overflow",
            )
            browser.close()
        require(not errors, errors)
        require(
            all(
                hashlib.sha256((source_dir / name).read_bytes()).hexdigest() == value
                for name, value in fixture_hashes.items()
            ),
            "An original changed",
        )
        report = {
            "python": platform.python_version(),
            "scenarios": reports,
            "fixture_sha256": fixture_hashes,
            "browser_errors": errors,
            "ada_review": "not_reviewed",
            "default_table_failure_reproduced": True,
            "scope": (
                "Explicit user-selected reading policy. "
                "Automatic table/prose inference is not repaired."
            ),
        }
        (root / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(
            json.dumps(
                {
                    "report": str(root / "report.json"),
                    "scenarios": len(reports),
                    "passed_selected_policy_checks": True,
                    "default_table_failure_reproduced": True,
                }
            )
        )
    finally:
        app.close()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
