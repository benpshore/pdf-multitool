"""Explicit author-selected order; automatic ambiguity remains a visible failure."""

import hashlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from experiments.representation_v1.build_fixtures import pdf, show
from experiments.table_prose_current_v1.accept import export_body
from pdf_multitool import analysis
from pdf_multitool.cli import main
from pdf_multitool.core import Job, JobError
from tests.test_extended import run

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "experiments/representation_v1"
NEW = ROOT / "experiments/row_order_contract_v1"
CONTRACTS = json.loads((NEW / "contracts.json").read_text())
EXPECTATIONS = json.loads((ORIGINAL / "expectations.json").read_text())["fixtures"]
EXPORTERS = {
    "txt": analysis.export_text,
    "md": analysis.export_markdown,
    "html": analysis.export_html,
}


def export(source, directory, mode):
    directory.mkdir()
    before = source.read_bytes()
    reports = {}
    with pymupdf.open(source) as document:
        for kind, function in EXPORTERS.items():
            reports[kind] = function(
                document, directory / f"export.{kind}", "anonymous.pdf", {"reading_order": mode}
            )
    assert source.read_bytes() == before
    return {kind: export_body(directory, kind) for kind in EXPORTERS}, reports


@pytest.mark.parametrize("case", list(EXPECTATIONS))
def test_original_oracles_with_selected_policy_and_unchanged_default(tmp_path, case):
    source = ORIGINAL / "fixtures" / f"{case}.pdf"
    expected = EXPECTATIONS[case]
    default, default_reports = export(source, tmp_path / "auto", "auto")
    if case.startswith("table_"):
        assert set(default.values()) == {"A1 A2 A3 B1 B2 B3"}
        assert all(value != expected for value in default.values())  # Still a default failure.
        selected, reports = export(source, tmp_path / "rows", "rows")
        assert all(value == expected for value in selected.values())
        for value in selected.values():
            assert all(f"A{i} B{i}" in value for i in (1, 2, 3))
        assert all(report["reading_order"] == "rows" for report in reports.values())
    else:
        assert all(value == expected for value in default.values())
    assert all(report["semantics_verified"] is False for report in default_reports.values())


@pytest.mark.parametrize("case", [name for name in CONTRACTS if name != "protocol"])
@pytest.mark.parametrize("mode,expected_key", [("rows", "row_text"), ("columns", "column_text")])
def test_frozen_numeric_prose_and_ambiguous_grids(tmp_path, case, mode, expected_key):
    source = NEW / "fixtures" / f"{case}.pdf"
    expected = CONTRACTS[case][expected_key]
    observed, reports = export(source, tmp_path / "observed", mode)
    assert all(value == expected for value in observed.values())
    assert all(Counter(value.split()) == Counter(expected.split()) for value in observed.values())
    assert all(report["semantics_verified"] is False for report in reports.values())
    assert all(
        any("explicitly selected" in warning for warning in report["warnings"])
        for report in reports.values()
    )
    if case.startswith("ambiguous_grid"):
        assert CONTRACTS[case]["row_text"] != CONTRACTS[case]["column_text"]
        assert CONTRACTS[case]["intent"] == "both_possible"


def test_short_prose_default_is_not_reclassified_by_token_length(tmp_path):
    for name in ("short_column_prose_rows", "short_column_prose_columns", "ambiguous_grid_rows"):
        observed, _ = export(NEW / "fixtures" / f"{name}.pdf", tmp_path / name, "auto")
        assert all(value == CONTRACTS[name]["column_text"] for value in observed.values())


def test_more_than_legacy_depth_and_tightly_spaced_numeric_rows(tmp_path):
    cells = [(f"{i:03d}", f"{i * 13}.25") for i in range(40)]
    expected = " ".join(" ".join(row) for row in cells)
    placements = [
        (text, x, 740 - i * 16)
        for i, row in enumerate(cells)
        for text, x in zip(row, (40, 330), strict=True)
    ]
    source = tmp_path / "large.pdf"
    source.write_bytes(pdf("".join(show(*p) for p in placements[::-1])))
    observed, _ = export(source, tmp_path / "rows", "rows")
    assert all(value == expected for value in observed.values())


def test_repeated_occurrences_are_not_collapsed_in_explicit_modes(tmp_path):
    for mode in ("rows", "columns"):
        observed, _ = export(ORIGINAL / "fixtures/form_repeated.pdf", tmp_path / mode, mode)
        assert set(observed.values()) == {"STAMP STAMP"}


@pytest.mark.parametrize("bad", [None, True, 1, [], {}, "", "table", "Rows"])
def test_invalid_policy_is_rejected_before_job_creation(tmp_path, bad):
    with pytest.raises(JobError, match="reading_order"):
        Job("textlayout", [], {"reading_order": bad}, tmp_path / "jobs")
    assert not (tmp_path / "jobs").exists()


def test_unsupported_operation_cannot_silently_ignore_policy(tmp_path):
    with pytest.raises(JobError, match="reading_order"):
        Job("text", [], {"reading_order": "rows"}, tmp_path / "jobs")
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize("fixture", ["rotated_90", "mixed_directions", "mixed_within_word"])
def test_unsupported_direction_fails_without_published_output_and_recovers(tmp_path, fixture):
    source = ROOT / "experiments/ltr_boundaries_v1/fixtures" / (fixture + ".pdf")
    failed = run(tmp_path, "textlayout", source, {"reading_order": "rows"})
    assert failed.state == "failed"
    assert failed.results == []
    good = run(
        tmp_path,
        "textlayout",
        ORIGINAL / "fixtures/table_column_operators.pdf",
        {"reading_order": "rows"},
    )
    assert good.state == "completed"
    assert EXPECTATIONS["table_column_operators"] in " ".join(
        Path(good.results[0]["path"]).read_text().split()
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_explicit_mode_rejects_nonfinite_geometry(value):
    page = SimpleNamespace(rotation=0, rect=SimpleNamespace(width=600))
    page.get_text = lambda *args, **kwargs: {
        "blocks": [{"type": 0, "bbox": [value, 10, 30, 40], "lines": []}]
    }
    with pytest.raises(ValueError, match="finite"):
        analysis._ordered_blocks(page, reading_order="rows")


@pytest.mark.parametrize(
    "operation,suffix", [("textlayout", "txt"), ("markdown", "md"), ("pdfhtml", "html")]
)
def test_cli_uses_selected_policy_and_preserves_original(tmp_path, capsys, operation, suffix):
    source = ORIGINAL / "fixtures/table_column_operators.pdf"
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    assert (
        main([operation, str(source), "--reading-order", "rows", "-o", str(tmp_path / "output")])
        == 0
    )
    job = json.loads(capsys.readouterr().out)
    assert job["state"] == "completed"
    output = next((tmp_path / "output").rglob("*." + suffix))
    case = tmp_path / "read"
    case.mkdir()
    (case / f"export.{suffix}").write_bytes(output.read_bytes())
    assert export_body(case, suffix) == EXPECTATIONS["table_column_operators"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
