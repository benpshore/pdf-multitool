"""Real bounded analysis/search jobs and strict offline OCR prerequisites."""

import hashlib
import importlib.metadata
import json
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
from pikepdf import Array, Dictionary, Name, Pdf, Stream

from pdf_multitool import oxide, oxide_analysis
from pdf_multitool.catalog import validate_options
from pdf_multitool.core import Job, JobError, Limits
from tests.test_extended import result, run, scan_fixture, source_fixture

ROOT = Path(__file__).resolve().parents[1]
NATIVE = pytest.mark.skipif(not oxide.availability()["available"], reason="Optional bounded Oxide")


def _read(job, name):
    return json.loads(result(job, name).read_text())


@pytest.mark.parametrize(
    "options",
    [
        {"filters_json": '{"region":[0,0,20,20],"region":[0,0,30,30]}'},
        {"filters_json": '{"region":[0,0,NaN,20]}'},
        {"filters_json": '{"unknown":true}'},
        {"mode": "chars", "filters_json": '{"region":[0,0,-1,20]}'},
        {"mode": "chars", "filters_json": '{"exclude_layers":["x",1]}'},
        {"mode": "spans", "span_order": "structure", "filters_json": '{"region":[0,0,10,10]}'},
        {"mode": "structured", "filters_json": '{"region":[0,0,10,10]}'},
        {"mode": "all", "profile": "academic"},
        {"mode": "chars", "include_artifacts": False},
        {"mode": "words", "filters_json": '{"word_gap_threshold":0}'},
    ],
)
def test_analysis_invalid_options_fail_before_directory(tmp_path, options):
    with pytest.raises(JobError):
        Job("oxide_analyze", [{"id": "x"}], options, tmp_path / "jobs")
    assert not (tmp_path / "jobs").exists()


@NATIVE
def test_analysis_real_geometry_content_and_provenance(tmp_path):
    source = source_fixture(tmp_path / "source.pdf")
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    job = run(tmp_path, "oxide_analyze", source, {"pages": "1"})
    assert job.state in {"completed", "partial"}, job.errors
    page = _read(job, "analysis-page-0001.json")
    analyses = page["analysis"]
    assert all(value["status"] != "error" for value in analyses.values()), analyses
    assert "Synthetic original" in " ".join(v["text"] for v in analyses["spans"]["native"])
    assert "10" in " ".join(v["text"] for v in analyses["words"]["native"])
    assert analyses["chars"]["native"][0]["char"] == "S"
    assert len(analyses["chars"]["native"][0]["bbox"]) == 4
    assert analyses["paths"]["native"]
    assert analyses["lines"]["native"]
    assert all(
        path["operations_count"] == len(path["operations"]) for path in analyses["paths"]["native"]
    )
    assert analyses["page_text"]["native"]["page_width"] == 400
    assert analyses["classification"]["native"]["has_text_layer"] is True
    assert analyses["rich_text"]["native"]["page"] == 0
    assert page["source_sha256"] == original and page["page"] == 1
    manifest = _read(job, "oxide-analysis-manifest.json")
    assert len(manifest["profile_catalog"]) == 9
    assert manifest["runtime_capabilities"]["model_downloads_allowed"] is False
    assert "languages" in manifest["runtime_capabilities"]["upstream_model_registry"]
    assert manifest["ocr"] == "not_run" and manifest["semantics_verified"] is False
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original


@NATIVE
@pytest.mark.parametrize("profile", oxide_analysis.PROFILES)
def test_each_profile_reaches_real_word_extraction(tmp_path, profile):
    source = source_fixture(tmp_path / "source.pdf")
    job = run(
        tmp_path, "oxide_analyze", source, {"mode": "words", "profile": profile, "pages": "1"}
    )
    assert job.state == "completed", job.errors
    words = _read(job, "analysis-page-0001.json")["analysis"]["words"]
    assert words["status"] == "extracted"
    assert {"Alpha", "Beta", "10", "20"} <= {item["text"] for item in words["native"]}


@NATIVE
def test_region_filter_is_intersection_and_preserves_source(tmp_path):
    source = source_fixture(tmp_path / "source.pdf")
    job = run(
        tmp_path,
        "oxide_analyze",
        source,
        {"mode": "spans", "pages": "1", "filters_json": '{"region":[0,320,400,80]}'},
    )
    assert job.state == "completed", job.errors
    spans = _read(job, "analysis-page-0001.json")["analysis"]["spans"]["native"]
    assert [item["text"] for item in spans] == ["Synthetic original"]
    assert all(len(item["bbox"]) == 4 and len(item["page_bbox"]) == 4 for item in spans)


@NATIVE
@pytest.mark.parametrize("order", ["top_to_bottom", "column_aware", "structure"])
def test_reading_order_modes_preserve_frozen_numbers(tmp_path, order):
    source = ROOT / "experiments/representation_v1/fixtures/table_column_operators.pdf"
    job = run(tmp_path, "oxide_analyze", source, {"mode": "spans", "span_order": order})
    assert job.state == "completed", job.errors
    spans = _read(job, "analysis-page-0001.json")["analysis"]["spans"]["native"]
    assert sorted(item["text"] for item in spans) == ["A1", "A2", "A3", "B1", "B2", "B3"]


@NATIVE
def test_scan_and_blank_classification_are_distinct(tmp_path):
    scan = scan_fixture(tmp_path / "scan.pdf")
    scanned = run(tmp_path, "oxide_analyze", scan, {"mode": "classification"})
    assert scanned.state == "completed", scanned.errors
    scanned_value = _read(scanned, "analysis-page-0001.json")["analysis"]["classification"][
        "native"
    ]
    assert scanned_value["has_text_layer"] is False
    assert scanned_value["page"]["kind"] == "scanned"
    blank = tmp_path / "blank.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(blank)
    empty = run(tmp_path, "oxide_analyze", blank, {"mode": "classification"})
    assert empty.state == "completed", empty.errors
    empty_value = _read(empty, "analysis-page-0001.json")["analysis"]["classification"]["native"]
    assert empty_value["page"]["kind"] == "empty"
    assert empty_value["has_text_layer"] is False


@NATIVE
def test_empty_layout_error_is_visible_not_success(tmp_path):
    source = tmp_path / "empty.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(source)
    job = run(tmp_path, "oxide_analyze", source)
    page = _read(job, "analysis-page-0001.json")
    assert page["analysis"]["spans"]["status"] == "empty"
    assert page["analysis"]["layout"]["status"] == "error"
    assert _read(job, "oxide-analysis-manifest.json")["partial"] is True
    assert job.state == "partial"


@NATIVE
def test_document_classification_does_not_ignore_large_unselected_tail(tmp_path):
    source = tmp_path / "many.pdf"
    with pymupdf.open() as doc:
        for _ in range(101):
            doc.new_page()
        doc.save(source)
    job = run(tmp_path, "oxide_analyze", source, {"pages": "1", "document_classification": True})
    assert job.state == "failed" and not job.results
    assert "100 pages" in str(job.errors)


@NATIVE
@pytest.mark.parametrize("scope,prepare", [("selected_pages", False), ("document", True)])
def test_real_search_matches_include_coordinates(tmp_path, scope, prepare):
    source = source_fixture(tmp_path / "source.pdf")
    job = run(
        tmp_path,
        "oxide_search",
        source,
        {
            "pattern": "alpha",
            "case_insensitive": True,
            "whole_word": True,
            "scope": scope,
            "prepare_index": prepare,
        },
    )
    assert job.state == "completed", job.errors
    report = _read(job, "oxide-search.json")
    assert len(report["matches"]) == 1
    match = report["matches"][0]
    assert match["native"]["text"] == "Alpha" and match["page"] == 1
    assert match["native"]["width"] > 0 and match["native"]["height"] > 0
    assert report["truncated"] is False


@NATIVE
def test_regex_search_and_invalid_expression(tmp_path):
    source = source_fixture(tmp_path / "source.pdf")
    good = run(tmp_path, "oxide_search", source, {"pattern": r"\b(?:10|20)\b", "literal": False})
    assert good.state == "completed", good.errors
    assert {x["native"]["text"] for x in _read(good, "oxide-search.json")["matches"]} == {
        "10",
        "20",
    }
    bad = run(tmp_path, "oxide_search", source, {"pattern": "[", "literal": False})
    assert bad.state == "failed" and not bad.results


@NATIVE
def test_match_and_output_overflow_fail_without_partial_exports(tmp_path):
    source = source_fixture(tmp_path / "source.pdf")
    limited = run(
        tmp_path,
        "oxide_search",
        source,
        {"pattern": "a", "case_insensitive": True, "max_results": 1},
    )
    assert limited.state == "failed" and not limited.results
    tiny = run(tmp_path, "oxide_analyze", source, limits=replace(Limits(), max_output_bytes=100))
    assert tiny.state == "failed" and not tiny.results


def _models(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    manifest = {
        "schema_version": 1,
        "engine_version": "0.3.78",
        "onnxruntime_version": "1.31.0",
        "language": "english",
        "files": {},
    }
    for role in ("detector", "recognizer", "dictionary"):
        data = b"synthetic manifest integrity bytes; not a trained model " + role.encode()
        (root / role).write_bytes(data)
        manifest["files"][role] = {
            "file": role,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "license": "Synthetic test data",
            "source": "Authored in this test; no inference claim",
        }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root, manifest


def test_ocr_gate_never_downloads_models(tmp_path, monkeypatch):
    monkeypatch.delenv("PDF_MULTITOOL_OXIDE_MODELS", raising=False)
    with pytest.raises(JobError, match="locally provisioned"):
        oxide_analysis.model_manifest()
    assert not oxide_analysis.ocr_availability()["available"]
    source = source_fixture(tmp_path / "source.pdf")
    job = run(tmp_path, "oxide_ocr", source)
    assert job.state == "failed" and not job.results


def test_local_manifest_validates_every_hash_and_refuses_symlinks(tmp_path):
    root, manifest = _models(tmp_path)
    assert oxide_analysis.model_manifest(str(root), verify_files=True)[1] == manifest
    (root / "recognizer").write_bytes(b"replaced bytes")
    with pytest.raises(JobError, match="pinned size/SHA-256"):
        oxide_analysis.model_manifest(str(root), verify_files=True)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(JobError, match="symlink"):
        oxide_analysis.model_manifest(str(link))


@NATIVE
def test_ocr_rejects_nonmodel_bytes_after_verified_offline_snapshot(tmp_path, monkeypatch):
    try:
        version = importlib.metadata.version("onnxruntime")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("Optional pinned ONNX Runtime is absent")
    if version != oxide_analysis.ONNXRUNTIME_VERSION:
        pytest.skip("Optional pinned ONNX Runtime is absent")
    root, _ = _models(tmp_path)
    monkeypatch.setenv("PDF_MULTITOOL_OXIDE_MODELS", str(root))
    source = source_fixture(tmp_path / "source.pdf")
    job = run(tmp_path, "oxide_ocr", source)
    assert job.state == "failed" and not job.results
    assert "offline OCR model load" in str(job.errors), job.errors
    assert list((Path(job.directory)).iterdir()) == []


@pytest.mark.parametrize("mutation", ["path", "digest", "version", "license", "unknown"])
def test_model_manifest_rejects_unpinned_or_unsafe_entries(tmp_path, mutation):
    root, manifest = _models(tmp_path)
    if mutation == "path":
        manifest["files"]["detector"]["file"] = "../detector"
    elif mutation == "digest":
        manifest["files"]["detector"]["sha256"] = "latest"
    elif mutation == "version":
        manifest["engine_version"] = "latest"
    elif mutation == "license":
        manifest["files"]["detector"]["license"] = ""
    else:
        manifest["download_url"] = "https://example.invalid/model"
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(JobError):
        oxide_analysis.model_manifest(str(root))


def test_analysis_native_page_count_failure_is_not_converted_to_empty(monkeypatch):
    from pdf_multitool import oxide_runtime

    class BadDocument:
        @property
        def page_count(self):
            raise RuntimeError("metadata failure")

    class Module:
        class PdfDocument:
            @staticmethod
            def from_bytes(*_):
                return BadDocument()

    monkeypatch.setattr(oxide_runtime, "require_runtime", lambda _: Module)
    monkeypatch.setattr(oxide_runtime, "read_input", lambda _: b"%PDF-1.7\n")
    with pytest.raises(JobError, match="page count"):
        oxide_runtime.open_source({}, {})


def test_serialization_limits_are_errors_not_truncation():
    budget = oxide_analysis._ContentBudget()
    with pytest.raises(JobError, match="text limit"):
        budget.convert("x" * (oxide_analysis.MAX_TEXT + 1))
    with pytest.raises(JobError, match="non-finite"):
        oxide_analysis._ContentBudget().convert(float("inf"))
    with pytest.raises(JobError, match="unsupported analysis"):
        oxide_analysis._ContentBudget().convert(object())


def test_search_preflight():
    with pytest.raises(JobError, match="nonempty"):
        validate_options("oxide_search", {})
    with pytest.raises(JobError, match="page selection"):
        validate_options("oxide_search", {"pattern": "x", "scope": "document", "pages": "1"})


@NATIVE
def test_layer_exclusions_apply_to_character_analysis(tmp_path):
    source = tmp_path / "layers.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=300, height=300)
        ocg = doc.add_ocg("Optional notes")
        page.insert_text((30, 40), "Baseline 012")
        page.insert_text((30, 80), "Optional 999", oc=ocg)
        doc.save(source)
    layers = run(tmp_path, "oxide_analyze", source, {"mode": "layers_inks"})
    names = _read(layers, "analysis-page-0001.json")["analysis"]["layers_inks"]["native"]
    assert "Optional notes" in names["document_layers"]
    filtered = run(
        tmp_path,
        "oxide_analyze",
        source,
        {"mode": "chars", "filters_json": '{"exclude_layers":["Optional notes"]}'},
    )
    characters = _read(filtered, "analysis-page-0001.json")["analysis"]["chars"]["native"]
    text = "".join(item["char"] for item in characters)
    assert "012" in text and "999" not in text


@NATIVE
def test_deep_ink_inspection_reaches_form_resources(tmp_path):
    source = tmp_path / "nested-spot.pdf"
    with Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 300))
        tint = Dictionary(
            FunctionType=2,
            Domain=Array([0, 1]),
            C0=Array([0, 0, 0, 0]),
            C1=Array([0, 0.5, 1, 0]),
            N=1,
        )
        form = Stream(pdf, b"/Spot cs 1 scn 20 20 50 50 re f")
        form.Type = Name.XObject
        form.Subtype = Name.Form
        form.BBox = Array([0, 0, 300, 300])
        form.Resources = Dictionary(
            ColorSpace=Dictionary(
                Spot=Array(
                    [
                        Name.Separation,
                        Name("/TestOrange"),
                        Name.DeviceCMYK,
                        tint,
                    ]
                )
            )
        )
        page.Resources = Dictionary(XObject=Dictionary(Fm1=form))
        page.Contents = Stream(pdf, b"q /Fm1 Do Q")
        pdf.save(source)
    job = run(tmp_path, "oxide_analyze", source, {"mode": "layers_inks"})
    assert job.state == "completed", job.errors
    inks = _read(job, "analysis-page-0001.json")["analysis"]["layers_inks"]["native"]
    assert "TestOrange" in inks["page_inks_deep"]


@NATIVE
@pytest.mark.parametrize("kind", ["region", "regions"])
def test_extraction_masks_preserve_original_and_unrelated_numbers(tmp_path, kind):
    source = source_fixture(tmp_path / "source.pdf")
    job = run(
        tmp_path,
        "oxide_extract_mask",
        source,
        {
            "actions_json": json.dumps(
                [{"action": kind, "page": 1, "rectangles": [[0, 320, 400, 400]]}]
            )
        },
    )
    assert job.state == "completed", job.errors
    first = _read(job, "masked-page-0001.json")
    assert "Synthetic original" in first["original"]["text"]
    assert "Synthetic original" not in first["derived"]["text"]
    assert "10" in first["derived"]["text"] and "20" in first["derived"]["text"]
    second = _read(job, "masked-page-0002.json")
    assert second["original"] == second["derived"]
    manifest = _read(job, "oxide-mask-manifest.json")
    assert manifest["secure_redaction"] is False and manifest["source_modified"] is False
    assert manifest["pdf_derivative_written"] is False
    assert not any(item["path"].endswith(".pdf") for item in job.results)


@NATIVE
def test_clearing_extraction_mask_restores_identical_text_and_spans(tmp_path):
    source = source_fixture(tmp_path / "source.pdf")
    job = run(
        tmp_path,
        "oxide_extract_mask",
        source,
        {
            "actions_json": json.dumps(
                [
                    {"action": "region", "page": 1, "rectangles": [[0, 320, 400, 400]]},
                    {"action": "clear", "page": 1},
                ]
            )
        },
    )
    assert job.state == "completed", job.errors
    page = _read(job, "masked-page-0001.json")
    assert page["original"] == page["derived"] and page["text_changed"] is False


@NATIVE
@pytest.mark.parametrize("kind", ["header", "footer", "artifacts"])
def test_native_header_footer_masks_are_explicit_derived_copies(tmp_path, kind):
    source = tmp_path / "running.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=400)
        page.insert_text((30, 20), "Meaningful top citation 012")
        page.insert_text((30, 190), "Body amount 10.25")
        page.insert_text((30, 385), "Meaningful footnote 036")
        doc.save(source)
    job = run(
        tmp_path,
        "oxide_extract_mask",
        source,
        {"actions_json": json.dumps([{"action": kind, "page": 1}])},
    )
    assert job.state == "completed", job.errors
    page = _read(job, "masked-page-0001.json")
    assert "012" in page["original"]["text"] and "036" in page["original"]["text"]
    assert "10.25" in page["derived"]["text"]
    assert page["text_changed"] is True
    assert "not been redacted" in " ".join(job.warnings)


@pytest.mark.parametrize(
    "actions",
    [
        [],
        [{"action": "write", "page": 1}],
        [{"action": "header", "page": 0}],
        [{"action": "region", "page": 1, "rectangles": [[10, 10, 0, 0]]}],
        [{"action": "header", "page": 1, "rectangles": [[0, 0, 10, 10]]}],
    ],
)
def test_mask_validation_is_pre_spawn(tmp_path, actions):
    with pytest.raises(JobError):
        Job(
            "oxide_extract_mask",
            [{"id": "x"}],
            {"actions_json": json.dumps(actions)},
            tmp_path / "jobs",
        )
    assert not (tmp_path / "jobs").exists()


@NATIVE
@pytest.mark.parametrize("mode", ["text", "tables"])
def test_region_text_and_table_candidates_use_exact_native_region_api(tmp_path, mode):
    source = source_fixture(tmp_path / "source.pdf")
    if mode == "text":
        with pytest.raises(JobError, match="out-of-region table"):
            run(
                tmp_path,
                "oxide_analyze",
                source,
                {"mode": mode, "pages": "1", "filters_json": '{"region":[0,320,400,80]}'},
            )
        return
    job = run(
        tmp_path,
        "oxide_analyze",
        source,
        {"mode": mode, "pages": "1", "filters_json": '{"region":[0,320,400,80]}'},
    )
    assert job.state == "completed", job.errors
    value = _read(job, "analysis-page-0001.json")["analysis"][mode]["native"]
    assert value == []


@NATIVE
def test_region_image_inspection_is_metadata_only(tmp_path):
    source = scan_fixture(tmp_path / "scan.pdf")
    job = run(
        tmp_path,
        "oxide_analyze",
        source,
        {"mode": "images", "filters_json": '{"region":[0,0,750,400]}'},
    )
    assert job.state == "completed", job.errors
    value = _read(job, "analysis-page-0001.json")["analysis"]["images"]["native"]
    assert len(value) == 1 and value[0]["width"] == 1500 and value[0]["height"] == 800
    assert all(item["path"].endswith(".json") for item in job.results)


def test_region_text_cannot_silently_ignore_artifact_exclusion():
    with pytest.raises(JobError, match="ignores artifact"):
        validate_options(
            "oxide_analyze",
            {"mode": "text", "include_artifacts": False, "filters_json": '{"region":[0,0,10,10]}'},
        )
