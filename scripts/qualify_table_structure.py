"""Actual GUI cell-structure evidence; ambiguous layouts remain explicit failures."""

import argparse
import hashlib
import html
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pypdfium2

from experiments.table_prose_current_v1.accept import export_body
from pdf_multitool.server import BoundedHTTPServer, Workbench, make_handler
from tests.test_table_structure import CONTRACTS, rendered_grid

REPO = Path(__file__).resolve().parents[1]
CASES = [
    ("representation_v1", "table_row_operators", "rows"),
    ("representation_v1", "table_column_operators", "rows"),
    ("representation_v1", "table_column_operators", "auto"),
    ("structured_cells_v1", "numeric_ruled_reverse", "auto"),
    ("structured_cells_v1", "missing_ruled_reverse", "auto"),
    ("structured_cells_v1", "wrapped_ruled_reverse", "auto"),
    ("structured_cells_v1", "empty_row_ruled_reverse", "auto"),
    ("structured_cells_v1", "missing_borderless_reverse", "auto"),
    ("structured_cells_v1", "missing_borderless_reverse", "rows"),
    ("structured_cells_v1", "wrapped_borderless_reverse", "auto"),
    ("structured_cells_v1", "wrapped_borderless_reverse", "rows"),
    ("structured_cells_v1", "wrapped_numeric_prose_reverse", "auto"),
]


def require(condition, message):
    if not condition:
        raise RuntimeError(str(message))


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    root = parser.parse_args().output.resolve()
    pandoc = shutil.which("pandoc")
    require(pandoc, "Pandoc is required to render the actual Markdown output")
    root.mkdir(parents=True, exist_ok=False)
    sources = root / "originals"
    sources.mkdir()
    expected_original = json.loads(
        (REPO / "experiments/representation_v1/expectations.json").read_text()
    )["fixtures"]
    source_hashes = {}
    for corpus, name, _ in CASES:
        target = sources / (name + ".pdf")
        if target.exists():
            continue
        shutil.copy2(REPO / "experiments" / corpus / "fixtures" / target.name, target)
        source_hashes[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
        with pypdfium2.PdfDocument(target) as doc:
            page = doc.get_page(0)
            textpage = page.get_textpage()
            (sources / (name + ".pdfium.txt")).write_text(textpage.get_text_range())
            textpage.close()
            for dpi in (72, 144, 288):
                bitmap = page.render(scale=dpi / 72)
                bitmap.to_pil().save(sources / f"{name}-{dpi}dpi.png")
                bitmap.close()
            page.close()
    session = root / "session"
    session.mkdir()
    app = Workbench(session)
    server = BoundedHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    records, errors = [], []
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
            viewer = browser.new_context(
                viewport={"width": 1100, "height": 800}, device_scale_factor=2
            )
            viewer.route("**/*", lambda route: route.abort())
            view = viewer.new_page()
            previous = None
            for corpus, name, mode in CASES:
                if name != previous:
                    if previous:
                        page.locator("#remove-selected").click()
                        expect(page.locator("#file-count")).to_have_text("0")
                    page.locator("#file-input").set_input_files(sources / (name + ".pdf"))
                    expect(page.locator("#file-count")).to_have_text("1")
                    previous = name
                contract = CONTRACTS.get(name)
                oracle = (
                    contract["authored_cells"]
                    if contract
                    else [expected_original[name].split()[i : i + 2] for i in range(0, 6, 2)]
                )
                for operation, suffix in (
                    ("textlayout", "txt"),
                    ("markdown", "md"),
                    ("pdfhtml", "html"),
                ):
                    page.select_option("#operation", "text")
                    page.select_option("#text-export", operation)
                    page.select_option("#reading-order", mode)
                    page.locator("#run-button").click()
                    expect(page.locator("#cancel-job")).to_be_hidden(timeout=30000)
                    expect(page.locator("#run-button")).to_be_enabled()
                    job = list(app.jobs.values())[-1]
                    require(job.state == "completed", job.snapshot())
                    directory = root / "outputs" / (name + "-" + mode) / suffix
                    directory.mkdir(parents=True)
                    for result in job.results:
                        with page.expect_download() as event:
                            page.get_by_role(
                                "button", name="Download " + result["name"], exact=True
                            ).click()
                        data = Path(event.value.path()).read_bytes()
                        require(
                            data == Path(result["path"]).read_bytes(),
                            "Download differs from published bytes",
                        )
                        filename = (
                            "export." + suffix
                            if result["name"].endswith("." + suffix)
                            else result["name"]
                        )
                        (directory / filename).write_bytes(data)
                    sidecar = next(directory.glob("*.tables.json"))
                    structure = json.loads(sidecar.read_text())
                    tables = [t for p in structure["pages"] for t in p["tables"]]
                    require(len(tables) == 1, "Expected one captured candidate or structured grid")
                    table = tables[0]
                    content = (directory / ("export." + suffix)).read_text()
                    actual_grid = (
                        rendered_grid(content, suffix, len(oracle[0]))
                        if table["emitted_as_table"]
                        else None
                    )
                    if table["emitted_as_table"]:
                        require(
                            actual_grid == oracle,
                            "Rendered cell matrix differs from the frozen authored matrix",
                        )
                    elif contract and contract["author_intent"] == "prose_columns":
                        expected = " ".join(
                            " ".join(c.split()) for c in contract["authored_column_text"]
                        )
                        require(
                            export_body(directory, suffix) == expected,
                            "Prose column content/order differs",
                        )
                    else:
                        require(
                            table["disposition"] == "ambiguous_borderless",
                            "Unresolved cells need ambiguity status",
                        )
                    if suffix == "md":
                        rendered = subprocess.run(  # noqa: S603 -- fixed local converter and generated fixture output; no shell
                            [pandoc, "--from=gfm", "--to=html", str(directory / "export.md")],
                            capture_output=True,
                            text=True,
                            check=True,
                            timeout=15,
                        ).stdout
                        content = (
                            "<style>body{font:16px system-ui;padding:24px}"
                            "table{border-collapse:collapse}td{border:1px solid #aaa;"
                            "padding:12px;vertical-align:top}code{white-space:pre-wrap}</style>"
                            + rendered
                        )
                    elif suffix == "txt":
                        content = (
                            "<style>body{font:18px monospace;padding:24px}"
                            "pre{white-space:pre-wrap;tab-size:18}</style><pre>"
                            + html.escape(content)
                            + "</pre>"
                        )
                    view.set_content(content)
                    view.screenshot(path=directory / "render.png", full_page=True)
                    records.append(
                        {
                            "case": name,
                            "corpus": corpus,
                            "mode": mode,
                            "operation": operation,
                            "authored_cells": oracle,
                            "candidate_cells": table["rows"],
                            "rendered_cells": actual_grid,
                            "candidate_matches_authored_cells": table["rows"] == oracle
                            if oracle
                            else None,
                            "rendered_cells_match_authored": actual_grid == oracle
                            if oracle
                            else None,
                            "disposition": table["disposition"],
                            "ambiguous_row_grouping": table["ambiguous_row_grouping"],
                            "source": "originals/" + name + ".pdf",
                            "source_render": "originals/" + name + "-144dpi.png",
                            "output": str((directory / ("export." + suffix)).relative_to(root)),
                            "output_render": str((directory / "render.png").relative_to(root)),
                            "table_report": str(sidecar.relative_to(root)),
                            "warnings": job.warnings,
                            "semantics_verified": False,
                            "ada_review": "not_reviewed",
                        }
                    )
            page.reload()
            expect(page.locator("#results button")).to_have_count(len(job.results))
            require(len(app.jobs) == 36, "Reload submitted a job")
            page.set_viewport_size({"width": 390, "height": 844})
            require(
                page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
                "Mobile width overflow",
            )
            browser.close()
        require(not errors, errors)
        require(
            all(
                hashlib.sha256((sources / n).read_bytes()).hexdigest() == digest
                for n, digest in source_hashes.items()
            ),
            "An original changed",
        )
        report = {
            "scenarios": records,
            "source_sha256": source_hashes,
            "browser_errors": errors,
            "ada_review": "not_reviewed",
            "semantics_verified": False,
            "workflow_checks_passed": True,
            "rendered_cell_matches": sum(
                r["rendered_cells_match_authored"] is True for r in records
            ),
            "unresolved_table_outputs": sum(
                r["rendered_cells_match_authored"] is False for r in records
            ),
        }
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps({k: v for k, v in report.items() if k not in {"scenarios", "source_sha256"}})
        )
    finally:
        app.close()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
