"""Actual native format controls preserve source identity and extraction baselines."""

import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

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
