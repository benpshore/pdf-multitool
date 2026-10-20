"""Capture synthetic frozen sources and actual bounded PDF Oxide outputs for review."""

import argparse
import hashlib
import html
import json
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import pypdfium2

from pdf_multitool.core import Job

REPO = Path(__file__).resolve().parents[1]
CORPORA = (
    "representation_v1",
    "row_order_contract_v1",
    "structured_cells_v1",
    "ltr_boundaries_v1",
    "oxide_adapter_v1",
)


def expectations(corpus, name, contracts):
    item = contracts[name]
    if corpus == "representation_v1":
        return (
            item,
            [item.split()[i : i + 2] for i in range(0, 6, 2)]
            if name.startswith("table_")
            else None,
            not name.startswith("table_"),
        )
    if corpus == "row_order_contract_v1":
        intent = item["intent"]
        return (
            item["row_text"]
            if intent == "rows"
            else item["column_text"]
            if intent == "columns"
            else None,
            item["grid"] if intent == "rows" else None,
            intent == "columns",
        )
    if corpus == "structured_cells_v1":
        cells = item["authored_cells"]
        if cells is None:
            return (
                " ".join(" ".join(value.split()) for value in item["authored_column_text"]),
                None,
                True,
            )
        text = " ".join(" ".join(cell.split()) for row in cells for cell in row if cell)
        if item.get("surrounding_text"):
            text = "Before the grid " + text + " After the grid"
        return text, cells, False
    return None, None, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    records = []
    for corpus in CORPORA:
        directory = REPO / "experiments" / corpus
        target = root / "originals" / corpus
        target.mkdir(parents=True)
        contract_file = directory / (
            "expectations.json" if corpus == "representation_v1" else "contracts.json"
        )
        contracts = json.loads(contract_file.read_text())
        if corpus == "representation_v1":
            contracts = contracts["fixtures"]
        if corpus == "ltr_boundaries_v1":
            contracts = contracts["cases"]
        for name in (
            "contracts.json",
            "expectations.json",
            "FROZEN.json",
            "PRECAPTURE.json",
            "build_fixtures.py",
            "README.md",
        ):
            if (directory / name).is_file():
                shutil.copy2(directory / name, target / name)
        for source in sorted((directory / "fixtures").glob("*.pdf")):
            original = target / source.name
            shutil.copy2(source, original)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            dest = root / "cases" / corpus / source.stem
            dest.mkdir(parents=True)
            source_reports = []
            with pypdfium2.PdfDocument(source) as document:
                for index in range(len(document)):
                    page = document.get_page(index)
                    textpage = page.get_textpage()
                    extracted = textpage.get_text_range()
                    textpage.close()
                    (dest / f"source-page-{index + 1:04d}-pdfium.txt").write_text(extracted)
                    for dpi in (72, 144, 288):
                        bitmap = page.render(scale=dpi / 72)
                        bitmap.to_pil().save(dest / f"source-page-{index + 1:04d}-{dpi}dpi.png")
                        bitmap.close()
                    page.close()
                    source_reports.append({"page": index + 1, "pdfium_text": extracted})
            subprocess.run(  # noqa: S603 -- fixed renderer and synthetic repository fixture
                [
                    shutil.which("pdftotext"),
                    "-layout",
                    str(source),
                    str(dest / "source-poppler-layout.txt"),
                ],
                check=True,
                timeout=20,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            options = {"include_html": True}
            if source.stem.startswith("cleanup_"):
                options["cleanup"] = "both"
            if source.stem.startswith("images_"):
                options["images"] = "export"
            with tempfile.TemporaryDirectory(prefix="oxide-synthetic-capture-") as temporary:
                job = Job(
                    "oxide",
                    [{"id": "source", "name": source.name, "path": str(source)}],
                    options,
                    Path(temporary),
                )
                job.run()
                captured = []
                for item in job.results:
                    path = Path(item["path"])
                    shutil.copy2(path, dest / path.name)
                    captured.append(
                        {
                            "file": path.name,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "bytes": path.stat().st_size,
                        }
                    )
                outcome = {"state": job.state, "errors": job.errors, "warnings": job.warnings}
            expected, expected_cells, expects_no_tables = expectations(
                corpus, source.stem, contracts
            )
            page_path = dest / "page-0001.json"
            page = json.loads(page_path.read_text()) if page_path.exists() else None
            observed = " ".join((page["text"] or "").split()) if page else None
            actual_cells = (
                [
                    [[cell["text"] for cell in row["cells"]] for row in table["rows"]]
                    for table in page["tables"]
                ]
                if page
                else None
            )
            record = {
                "corpus": corpus,
                "case": source.stem,
                "source_sha256": digest,
                "source": str(original.relative_to(root)),
                "directory": str(dest.relative_to(root)),
                "outcome": outcome,
                "expected_ordered_text": expected,
                "observed_ordered_text": observed,
                "ordered_text_match": observed == expected if expected is not None else None,
                "missing_whitespace_tokens": dict(
                    Counter(expected.split()) - Counter((observed or "").split())
                )
                if expected is not None
                else None,
                "extra_whitespace_tokens": dict(
                    Counter((observed or "").split()) - Counter(expected.split())
                )
                if expected is not None
                else None,
                "expected_cells": expected_cells,
                "actual_cells": actual_cells,
                "exact_cell_match": actual_cells == [expected_cells]
                if expected_cells is not None
                else None,
                "prose_no_table_match": actual_cells == [] if expects_no_tables else None,
                "source_pages": source_reports,
                "outputs": captured,
                "semantics_verified": False,
                "ada_visual_review": "pending",
            }
            (dest / "accuracy.json").write_text(json.dumps(record, indent=2) + "\n")
            records.append(record)
            if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                raise RuntimeError("Original fixture changed")
            print(
                json.dumps(
                    {
                        "case": source.stem,
                        "state": outcome["state"],
                        "text": record["ordered_text_match"],
                        "cells": record["exact_cell_match"],
                    }
                ),
                flush=True,
            )
    summary = {
        "fixtures": len(records),
        "process_states": dict(Counter(row["outcome"]["state"] for row in records)),
        "ordered_text": dict(Counter(str(row["ordered_text_match"]) for row in records)),
        "exact_cells": dict(Counter(str(row["exact_cell_match"]) for row in records)),
        "prose_no_table": dict(Counter(str(row["prose_no_table_match"]) for row in records)),
        "semantics_verified": False,
        "ada_visual_review": "pending",
    }
    (root / "accuracy.json").write_text(json.dumps(records, indent=2) + "\n")
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    links = [
        "<!doctype html><meta charset=utf-8><title>PDF Oxide source/output review</title>",
        "<h1>Source/output review · Ada acceptance pending</h1><p>Every input is synthetic. "
        "Failed text/cell oracles are retained. Open the original PDF and each output; "
        "worker completion is not correctness.</p><ul>",
    ]
    for row in records:
        directory = html.escape(row["directory"], quote=True)
        links.append(
            f'<li><a href="{html.escape(row["source"], quote=True)}">'
            f"{html.escape(row['corpus'] + '/' + row['case'])}</a> · "
            f'<a href="{directory}/page-0001.html">output review</a> · '
            f"text={row['ordered_text_match']}, cells={row['exact_cell_match']} · "
            f'<a href="{directory}/accuracy.json">details</a></li>'
        )
    (root / "index.html").write_text("".join(links) + "</ul>")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
