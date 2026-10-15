"""Cell-level accuracy against frozen author grids, separate from ambiguity checks."""

import csv
import hashlib
import io
import json
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pymupdf
import pytest

from experiments.table_prose_current_v1.accept import export_body
from pdf_multitool import analysis, table_structure
from tests.test_extended import run

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "experiments/structured_cells_v1"
CONTRACTS = json.loads((CORPUS / "contracts.json").read_text())
EXPORTS = {
    "html": analysis.export_html,
    "md": analysis.export_markdown,
    "txt": analysis.export_text,
}


class Grids(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.grids = []
        self.grid = self.row = self.cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.grid = []
        elif tag == "tr" and self.grid is not None:
            self.row = []
        elif tag == "td" and self.row is not None:
            self.cell = []
        elif self.cell is not None and (
            tag == "br" or (tag == "span" and attrs.get("class") == "text-line" and self.cell)
        ):
            self.cell.append("\n")

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.row.append("".join(self.cell))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.grid.append(self.row)
            self.row = None
        elif tag == "table" and self.grid is not None:
            self.grids.append(self.grid)
            self.grid = None

    def handle_data(self, text):
        if self.cell is not None:
            self.cell.append(text)


def rendered_grid(text, kind, columns):
    if kind == "txt":
        rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
        return [row for row in rows if len(row) == columns]
    parser = Grids()
    parser.feed(text)
    assert len(parser.grids) == 1
    return parser.grids[0]


def exported(tmp_path, source, kind, mode="auto"):
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / ("export." + kind)
    with pymupdf.open(source) as doc:
        report = EXPORTS[kind](doc, output, source.name, {"reading_order": mode})
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    sidecars = [item for item in report["outputs"] if item["kind"] == "table_structure"]
    assert len(sidecars) == 1
    saved = json.loads(Path(sidecars[0]["path"]).read_text())
    assert saved["pages"] == report["table_structure"]
    assert saved["semantics_verified"] is False
    return report, output.read_text()


@pytest.mark.parametrize("case", [name for name, data in CONTRACTS.items() if data["ruled"]])
@pytest.mark.parametrize("kind", list(EXPORTS))
def test_automatic_ruled_cells_match_authored_grid_and_surrounding_text(tmp_path, case, kind):
    expected = CONTRACTS[case]["authored_cells"]
    report, content = exported(tmp_path, CORPUS / "fixtures" / (case + ".pdf"), kind)
    tables = [t for page in report["table_structure"] for t in page["tables"]]
    assert len(tables) == 1
    table = tables[0]
    assert table["disposition"] == "structured_ruled_grid"
    assert table["rows"] == expected
    assert rendered_grid(content, kind, len(expected[0])) == expected
    assert table["spacer_bands_omitted"] == []
    ids = [index for row in table["cells"] for cell in row for index in cell["source_line_ids"]]
    assert len(ids) == len(set(ids)) == table["source_line_occurrences"]
    assert table["semantics_verified"] is False
    assert not table["header_inferred"]
    if kind == "txt":
        # Decode TSV quoting as syntax, including literal newlines within a cell.
        # The old frozen whitespace scorer remains unchanged and is not a TSV parser.
        prefix = "[Source page 1]\n\n"
        assert content.startswith(prefix)
        records = csv.reader(io.StringIO(content[len(prefix) :]), delimiter="\t")
        body = " ".join(" ".join(cell.split()) for row in records for cell in row if cell)
    else:
        body = export_body(tmp_path, kind)
    expected_text = " ".join(" ".join(cell.split()) for row in expected for cell in row if cell)
    assert body == "Before the grid " + expected_text + " After the grid"


@pytest.mark.parametrize("case", [name for name, data in CONTRACTS.items() if not data["ruled"]])
def test_borderless_candidates_remain_ambiguous_and_preserve_prose(tmp_path, case):
    report, content = exported(tmp_path, CORPUS / "fixtures" / (case + ".pdf"), "html")
    tables = [t for page in report["table_structure"] for t in page["tables"]]
    assert tables and all(t["disposition"] == "ambiguous_borderless" for t in tables)
    assert not any(t["emitted_as_table"] for t in tables)
    assert "<table " not in content
    assert any("ambiguous" in warning for warning in report["warnings"])
    contract = CONTRACTS[case]
    if contract["author_intent"] == "prose_columns":
        expected = " ".join(" ".join(c.split()) for c in contract["authored_column_text"])
        assert export_body(tmp_path, "html") == expected
    elif case.startswith(("wrapped_", "empty_row_")):
        # These are known cell-association failures, not positive table qualification.
        assert tables[0]["rows"] != contract["authored_cells"]
    else:
        assert tables[0]["rows"] == contract["authored_cells"]


@pytest.mark.parametrize("case", ["table_row_operators", "table_column_operators"])
@pytest.mark.parametrize("kind", list(EXPORTS))
def test_original_borderless_rows_are_actual_cells_when_selected(tmp_path, case, kind):
    source = ROOT / "experiments/representation_v1/fixtures" / (case + ".pdf")
    report, content = exported(tmp_path, source, kind, "rows")
    expected = [["A1", "B1"], ["A2", "B2"], ["A3", "B3"]]
    assert rendered_grid(content, kind, 2) == expected
    table = report["table_structure"][0]["tables"][0]
    assert table["disposition"] == "user_selected_spatial_bands"
    assert table["rows"] == expected and not table["cell_boundaries_from_rules"]
    assert export_body(tmp_path, kind) == "A1 B1 A2 B2 A3 B3"


@pytest.mark.parametrize("kind", list(EXPORTS))
def test_selected_borderless_missing_cells_keep_empty_column_positions(tmp_path, kind):
    source = ROOT / "experiments/row_order_contract_v1/fixtures/missing_cells_reverse.pdf"
    _, content = exported(tmp_path, source, kind, "rows")
    assert rendered_grid(content, kind, 3) == [
        ["ID", "Amount", "Tax"],
        ["012", "", "0.15"],
        ["036", "2.00", ""],
    ]


def test_wrapped_borderless_rows_are_not_silently_promoted(tmp_path):
    report, content = exported(
        tmp_path, CORPUS / "fixtures/wrapped_borderless_rows.pdf", "html", "rows"
    )
    table = report["table_structure"][0]["tables"][0]
    assert table["ambiguous_row_grouping"] and not table["emitted_as_table"]
    assert table["rows"] != CONTRACTS["wrapped_borderless_rows"]["authored_cells"]
    assert "<table " not in content


def test_explicit_columns_still_uses_the_column_policy(tmp_path):
    report, content = exported(
        tmp_path, CORPUS / "fixtures/numeric_ruled_rows.pdf", "html", "columns"
    )
    assert not report["tables_exported"] and "<table " not in content
    assert report["table_structure"][0]["tables"][0]["disposition"] == "explicit_column_order"


def test_detection_failure_and_budget_keep_source_text(tmp_path, monkeypatch):
    source = CORPUS / "fixtures/numeric_ruled_rows.pdf"
    monkeypatch.setattr(table_structure, "MAX_LINES", 3)
    report, content = exported(tmp_path, source, "html")
    assert not report["tables_exported"]
    assert report["table_structure"][0]["skipped"] == "source-line budget exceeded"
    assert all(value in content for value in ("0007", "1,234.50", "-90.00", "100%"))


def test_existing_sidecar_is_preserved_on_repeat_export(tmp_path):
    source = CORPUS / "fixtures/numeric_ruled_rows.pdf"
    first, _ = exported(tmp_path, source, "html")
    old = Path(first["outputs"][1]["path"])
    data = old.read_bytes()
    second, _ = exported(tmp_path, source, "html")
    assert second["outputs"][1]["path"] != str(old)
    assert old.read_bytes() == data


def test_margin_filtering_keeps_its_original_text_path(tmp_path):
    source = CORPUS / "fixtures/missing_ruled_rows.pdf"
    with pymupdf.open(source) as doc:
        report = analysis.export_text(
            doc, tmp_path / "filtered.txt", source.name, {"skip_repeated_margins": True}
        )
    assert report["tables_exported"] == 0
    assert report["table_structure"][0]["tables"][0]["disposition"] == "text_path_for_margin_filter"
    assert any("honour requested" in warning for warning in report["warnings"])


def test_native_detector_error_keeps_all_source_occurrences(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("Injected native failure")

    monkeypatch.setattr(pymupdf.Page, "find_tables", broken)
    report, content = exported(tmp_path, CORPUS / "fixtures/numeric_ruled_rows.pdf", "html")
    assert report["table_structure"][0]["skipped"].endswith("RuntimeError")
    assert report["tables_exported"] == 0
    assert all(value in content for value in ("0007", "1,234.50", "-90.00", "100%"))


def test_merged_cells_are_rejected_instead_of_splitting_or_dropping_text(tmp_path):
    source = tmp_path / "merged.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=300)
        for y in (40, 80, 120, 160):
            page.draw_line((40, y), (340, y))
        for x in (40, 240, 340):
            page.draw_line((x, 40), (x, 160))
        page.draw_line((140, 80), (140, 160))
        for y, pairs in [
            (65, [(50, "Merged"), (250, "Right")]),
            (105, [(50, "11"), (150, "12"), (250, "13")]),
            (145, [(50, "21"), (150, "22"), (250, "23")]),
        ]:
            for x, text in pairs:
                page.insert_text((x, y), text)
        doc.save(source)
    report, _ = exported(tmp_path, source, "html")
    assert report["tables_exported"] == 0
    assert any(
        "merged" in r["reason"] for page in report["table_structure"] for r in page["rejected"]
    )
    observed = export_body(tmp_path, "html").split()
    assert sorted(observed) == sorted(["Merged", "Right", "11", "12", "13", "21", "22", "23"])


@pytest.mark.parametrize(
    "kind,include_images",
    [("html", True), ("html", False), ("md", True), ("md", False), ("txt", False)],
)
def test_image_overlap_blocks_projection_even_when_image_export_is_disabled(
    tmp_path, kind, include_images
):
    original = CORPUS / "fixtures/numeric_ruled_rows.pdf"
    source = tmp_path / "overlap.pdf"
    with pymupdf.open(original) as doc:
        # Add a real image occurrence inside the authored table rectangle.
        # Its geometry must remain relevant when image bytes are excluded.
        bounds = CONTRACTS["numeric_ruled_rows"]
        x, y = bounds["authored_column_bounds"][0], bounds["authored_row_bounds"][0]
        pix = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 2, 2))
        pix.clear_with(180)
        doc[0].insert_image((x + 1, y + 1, x + 3, y + 3), pixmap=pix)
        doc.save(source)
    output = tmp_path / ("export." + kind)
    options = {"include_images": include_images} if kind != "txt" else {}
    with pymupdf.open(source) as doc:
        result = EXPORTS[kind](doc, output, source.name, options)
    assert result["tables_exported"] == 0
    assert any(
        "overlaps an image" in rejection["reason"]
        for page in result["table_structure"]
        for rejection in page["rejected"]
    )
    # The frozen text-only Markdown scorer deliberately rejects image syntax.
    # This boundary check reads the new image-bearing output without changing it.
    observed = (
        unescape(re.sub(r"\\(.)", r"\1", output.read_text()))
        if kind == "md"
        else export_body(tmp_path, kind)
    )
    assert all(value in observed for value in ("0007", "1,234.50", "-90.00", "100%"))


@pytest.mark.parametrize("kind", list(EXPORTS))
def test_cell_markup_is_inert_and_repeated_occurrences_survive(tmp_path, kind):
    source = tmp_path / "literal.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=500, height=250)
        for y in (40, 90, 140):
            page.draw_line((40, y), (460, y))
        for x in (40, 250, 460):
            page.draw_line((x, 40), (x, 140))
        for x, y, text in [
            (50, 65, "STAMP"),
            (50, 65, "STAMP"),
            (260, 65, "</td><script>&"),
            (50, 115, "0007"),
            (260, 115, "-90.00"),
        ]:
            page.insert_text((x, y), text)
        doc.save(source)
    report, content = exported(tmp_path, source, kind)
    cells = rendered_grid(content, kind, 2)
    assert cells[0][0].count("STAMP") == 2
    assert cells[0][1] == "</td><script>&"
    assert cells[1] == ["0007", "-90.00"]
    assert report["table_structure"][0]["tables"][0]["source_line_occurrences"] == 5
    if kind != "txt":
        assert "<script>" not in content


@pytest.mark.parametrize(
    "operation,kind", [("pdfhtml", "html"), ("markdown", "md"), ("textlayout", "txt")]
)
def test_supervised_jobs_publish_structured_cells_without_changing_original(
    tmp_path, operation, kind
):
    source = CORPUS / "fixtures/wrapped_ruled_reverse.pdf"
    job = run(tmp_path, operation, source)
    assert job.state == "completed", job.errors
    sidecar = next(Path(r["path"]) for r in job.results if r["name"].endswith(".tables.json"))
    table = json.loads(sidecar.read_text())["pages"][0]["tables"][0]
    assert table["rows"] == CONTRACTS["wrapped_ruled_reverse"]["authored_cells"]
    assert any(r["name"].endswith("." + kind) for r in job.results)
    assert not any("Markdown images" in warning for warning in job.warnings)
