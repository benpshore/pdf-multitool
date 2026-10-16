"""Actual native format controls preserve source identity and extraction baselines."""

import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

from pdf_multitool import oxide_format
from pdf_multitool.catalog import validate_options
from pdf_multitool.core import JobError
from tests.test_extended import result, run

pytest.importorskip("pdf_oxide")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("kind", ["text", "markdown", "html"])
@pytest.mark.parametrize("layout", [False, True])
def test_actual_formats_preserve_numeric_tokens_and_original_baseline(tmp_path, kind, layout):
    source = ROOT / "experiments/structured_cells_v1/fixtures/numeric_ruled_reverse.pdf"
    job = run(tmp_path, "oxide_format", source, {"format": kind, "preserve_layout": layout})
    assert job.state == "completed", job.errors
    report = json.loads(result(job, "oxide-format-manifest.json").read_text())
    page = report["pages"][0]
    text = result(job, page["native_output"]["file"]).read_text()
    baseline = result(job, page["baseline"]["file"]).read_text()
    contracts = json.loads((ROOT / "experiments/structured_cells_v1/contracts.json").read_text())
    for row in contracts["numeric_ruled_reverse"]["authored_cells"]:
        for token in row:
            assert token in text and token in baseline
    assert report["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert page["native_output"]["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert report["applied_native_arguments"]["include_images"] is False
    assert report["semantics_verified"] is False and report["truncated"] is False
    assert report["partial"] is False and page["partial"] is False
    assert page["truncated"] is False
    if kind == "html":
        review = result(job, "page-0001.html").read_text()
        assert "default-src 'none'" in review and "<script" not in review
        assert report["source"]["sha256"] in review


def test_form_field_control_changes_only_additional_format_copy(tmp_path):
    source = tmp_path / "fields.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((40, 40), "Invoice 036 -2.50")
        widget = pymupdf.Widget()
        widget.field_name = "Account"
        widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        widget.field_value = "FIELD-001.20"
        widget.rect = pymupdf.Rect(40, 70, 180, 95)
        page.add_widget(widget)
        doc.save(source)
    texts, baselines = [], []
    for include in (False, True):
        job = run(tmp_path, "oxide_format", source, {"include_form_fields": include})
        assert job.state == "completed", job.errors
        texts.append(result(job, "page-0001.md").read_text())
        baselines.append(result(job, "page-0001.baseline.txt").read_text())
    assert "FIELD-001.20" not in texts[0] and "FIELD-001.20" in texts[1]
    assert baselines[0] == baselines[1] and "036" in baselines[0]


def test_tagged_artifact_control_retains_untouched_baseline(tmp_path):
    source = ROOT / "experiments/oxide_adapter_v1/fixtures/cleanup_mixed_tagged.pdf"
    outputs, baselines = [], []
    for include in (False, True):
        job = run(
            tmp_path,
            "oxide_format",
            source,
            {"format": "markdown", "pages": "1", "include_artifacts": include},
        )
        assert job.state == "completed", job.errors
        outputs.append(result(job, "page-0001.md").read_text())
        baselines.append(result(job, "page-0001.baseline.txt").read_text())
    assert baselines[0] == baselines[1]
    assert outputs[0] != outputs[1]
    assert len(outputs[0]) < len(outputs[1])


def operator_cap_source(path):
    """Valid text after the native cap remains independently extractable."""
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=300)
        page.insert_text((40, 50), "Before cap 001 -12.50")
        content = page.get_contents()[0]
        stream = (
            doc.xref_stream(content)
            + b"BT /helv 11 Tf 0 TL 1 0 0 1 40 200 Tm\n"
            + b"T*\n" * 1_000_010
            + b"(After cap 036 0.00) Tj ET\n"
        )
        doc.update_stream(content, stream)
        doc.save(path)
    return path


@pytest.mark.parametrize("kind", ["text", "markdown", "html"])
def test_actual_operator_cap_marks_format_and_baseline_incomplete(tmp_path, kind):
    source = operator_cap_source(tmp_path / "operator-cap.pdf")
    original = source.read_bytes()
    with pymupdf.open(source) as document:
        assert "After cap 036 0.00" in document[0].get_text()
    job = run(tmp_path, "oxide_format", source, {"format": kind})
    assert job.state == "partial", job.snapshot()
    report = json.loads(result(job, "oxide-format-manifest.json").read_text())
    page = report["pages"][0]
    assert report["truncated"] is True and report["partial"] is True
    assert page["truncated"] is True and page["partial"] is True
    assert page["diagnostic_status"] == "warnings_present"
    capped = [
        item
        for item in report["structured_warnings"]
        if item["native"]["category"] == "operator_cap_exceeded"
    ]
    assert capped and all(item["page"] is None for item in capped)
    assert all(item["native"]["spec_section"] == "Annex C" for item in capped)
    assert all(item["source_sha256"] == hashlib.sha256(original).hexdigest() for item in capped)
    for key in ("baseline", "native_output"):
        output = result(job, page[key]["file"]).read_text()
        assert "Before cap 001 -12.50" in output
        assert "After cap 036 0.00" not in output
        assert hashlib.sha256(output.encode()).hexdigest() == page[key]["sha256"]
    assert any(
        "operator cap" in warning and "omit source content" in warning for warning in job.warnings
    )
    assert source.read_bytes() == original


def test_global_operator_cap_marks_selected_pages_conservatively(tmp_path):
    source = operator_cap_source(tmp_path / "global-cap.pdf")
    with pymupdf.open(source) as document:
        document.new_page().insert_text((40, 50), "Unaffected page 002 15.00")
        document.saveIncr()
    job = run(tmp_path, "oxide_format", source)
    assert job.state == "partial", job.snapshot()
    report = json.loads(result(job, "oxide-format-manifest.json").read_text())
    assert len(report["pages"]) == 2
    # The native warning supplies no page, so claiming either selected page is
    # complete would discard the diagnostic's uncertainty.
    assert all(page["truncated"] and page["partial"] for page in report["pages"])
    assert "Unaffected page 002 15.00" in result(job, "page-0002.md").read_text()
    selected = run(tmp_path, "oxide_format", source, {"pages": "2"})
    assert selected.state == "completed", selected.snapshot()
    selected_report = json.loads(result(selected, "oxide-format-manifest.json").read_text())
    assert selected_report["truncated"] is False and selected_report["partial"] is False


def test_scan_diagnostic_is_page_scoped_and_not_a_truncation_claim(tmp_path):
    source = tmp_path / "text-and-scan.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((40, 50), "Native text 001 -12.50")
        scan = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
        scan.clear_with(180)
        page = doc.new_page()
        page.insert_image(page.rect, stream=scan.tobytes("png"))
        doc.save(source)
    job = run(tmp_path, "oxide_format", source)
    assert job.state == "partial", job.snapshot()
    report = json.loads(result(job, "oxide-format-manifest.json").read_text())
    assert report["partial"] is True and report["truncated"] is False
    first, second = report["pages"]
    assert first["partial"] is False and first["ocr_required"] is None
    assert second["partial"] is True and second["ocr_required"] is True
    assert second["truncated"] is False and second["native_output_empty"] is True
    assert any(
        item["page"] == 2 and item["native"]["category"] == "no_text_layer"
        for item in report["structured_warnings"]
    )
    assert any("OCR was not run" in warning for warning in job.warnings)
    selected = run(tmp_path, "oxide_format", source, {"pages": "1"})
    assert selected.state == "completed", selected.snapshot()


@pytest.mark.parametrize("category", ["glyph_dropped", "image_suppressed"])
@pytest.mark.parametrize("page", [None, 2])
def test_reported_content_omission_is_partial_without_claiming_operator_truncation(category, page):
    diagnostics = [
        {
            "page": page,
            "source_sha256": "synthetic-source",
            "native": {"category": category, "page": None if page is None else page - 1},
        }
    ]
    original = json.dumps(diagnostics)
    affected = oxide_format._page_diagnostics(diagnostics, 2)
    assert affected["partial"] is True and affected["truncated"] is False
    assert affected["omitted_content_categories"] == [category]
    assert affected["ocr_required"] is None
    first = oxide_format._page_diagnostics(diagnostics, 1)
    assert first["partial"] is (page is None)
    assert json.dumps(diagnostics) == original


@pytest.mark.parametrize("category", ["to_unicode_missing", "font", "layout"])
def test_fallback_diagnostics_remain_visible_without_claiming_known_omission(category):
    diagnostic = {"page": None, "native": {"category": category, "page": None}}
    status = oxide_format._page_diagnostics([diagnostic], 1)
    assert status["diagnostic_status"] == "warnings_present"
    assert status["partial"] is False and status["truncated"] is False
    assert status["omitted_content_categories"] == [] and status["ocr_required"] is None


@pytest.mark.parametrize(
    "options",
    [
        {"format": "html", "include_artifacts": False},
        {"format": "text", "include_form_fields": False},
        {"format": "text", "include_artifacts": False},
        {"include_images": True},
        {"image_output_dir": "/unexpected-native-export"},
        {"pages": "1-101"},
    ],
)
def test_unsupported_format_controls_fail_before_worker(options):
    with pytest.raises(JobError):
        validate_options("oxide_format", options)
