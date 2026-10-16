"""Real generated documents exercised through the same workers as HTTP and CLI."""

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from pdf_multitool.catalog import OPERATIONS, validate_options
from pdf_multitool.core import Job, JobError, Limits


def source_fixture(path):
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=400)
        page.insert_text((40, 60), "Synthetic original")
        for y in (100, 130, 160, 190):
            page.draw_line((40, y), (300, y))
        for x in (40, 170, 300):
            page.draw_line((x, 100), (x, 190))
        for y, values in ((120, ("Item", "Amount")), (150, ("Alpha", "10")), (180, ("Beta", "20"))):
            for x, text in zip((50, 180), values, strict=True):
                page.insert_text((x, y), text)
        page.insert_text((40, 240), "Duplicated")
        page.insert_text((40, 240), "Duplicated")
        page = doc.new_page(width=400, height=400)
        page.insert_text((40, 60), "Second page preserved")
        doc.embfile_add("../unsafe.wav", b"synthetic media bytes", filename="../../unsafe.wav")
        doc.set_toc([[1, "First page", 1]])
        doc.set_metadata({"title": "Synthetic qualification"})
        doc.save(path)
    return path


def scan_fixture(path, angle=0):
    image = Image.new("RGB", (1500, 800), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 42)
    for i, text in enumerate(
        ("SYNTHETIC OCR FIXTURE", "Invoice 12345", "Total amount 200 dollars")
    ):
        draw.text((140, 170 + i * 130), text, font=font, fill="black")
    image = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    with pymupdf.open() as doc:
        page = doc.new_page(width=image.width / 2, height=image.height / 2)
        page.insert_image(page.rect, stream=stream.getvalue())
        doc.save(path)
    return path


@pytest.fixture
def source(tmp_path):
    return source_fixture(tmp_path / "source.pdf")


def run(tmp_path, operation, source=None, options=None, limits=None):
    files = [] if source is None else [{"id": "f", "name": source.name, "path": str(source)}]
    before = source.read_bytes() if source else None
    job = Job(operation, files, options or {}, tmp_path / "jobs", limits or Limits())
    job.run()
    if source:
        assert source.read_bytes() == before, "Original bytes changed"
    return job


def result(job, suffix):
    paths = [Path(item["path"]) for item in job.results if item["path"].endswith(suffix)]
    assert len(paths) == 1, job.snapshot()
    return paths[0]


def test_create_unicode_pagination_and_headers(tmp_path):
    job = run(tmp_path, "create", options={"content": "Synthetic \u03b1 é Ж 中\n" * 75})
    assert job.state == "completed", job.errors
    with pymupdf.open(result(job, ".pdf")) as doc:
        assert len(doc) > 1
        assert "\u03b1 é Ж 中" in doc[0].get_text()
    header = run(tmp_path, "paginate", result(job, ".pdf"), {"header": "Review copy", "pages": "1"})
    assert header.state == "completed", header.errors
    with pymupdf.open(result(header, ".pdf")) as doc:
        assert "Review copy" in doc[0].get_text()
        assert f"Page 1 of {len(doc)}" in doc[0].get_text()
        assert "Review copy" not in doc[1].get_text()


def test_tables_preserve_row_pairs_and_csv(tmp_path, source):
    job = run(tmp_path, "tables", source, {"pages": "1"})
    assert job.state == "completed", job.errors
    report = json.loads(result(job, ".json").read_text())
    assert report["pages"][0]["tables"][0]["rows"] == [
        ["Item", "Amount"],
        ["Alpha", "10"],
        ["Beta", "20"],
    ]
    assert "Alpha,10" in result(job, ".csv").read_text()
    assert report["semantics_verified"] is False


def test_geometry_character_dedup_is_explicit_and_non_destructive(tmp_path, source):
    original = run(tmp_path, "geometry", source, {"pages": "1"})
    dedup = run(tmp_path, "geometry", source, {"pages": "1", "dedupe_chars": True})
    assert original.state == dedup.state == "completed"
    before = json.loads(result(original, ".json").read_text())["pages"][0]
    after = json.loads(result(dedup, ".json").read_text())["pages"][0]
    assert before["removed_duplicate_characters"] == 0
    assert after["removed_duplicate_characters"] == len("Duplicated")
    assert len(before["characters"]) - len(after["characters"]) == len("Duplicated")
    assert "Synthetic original" in result(dedup, ".txt").read_text()


@pytest.mark.parametrize(
    "operation, options",
    [
        ("optimize", {"compression_level": 1}),
        ("optimize", {"compression_level": 9, "linearize": True}),
        ("deduplicate", {}),
    ],
)
def test_lossless_rewrites_preserve_pages_text_attachments_and_render(
    tmp_path, source, operation, options
):
    job = run(tmp_path, operation, source, options)
    assert job.state == "completed", job.errors
    with pymupdf.open(source) as before, pymupdf.open(result(job, ".pdf")) as after:
        assert len(after) == len(before)
        assert after.embfile_get(0) == before.embfile_get(0)
        assert after.get_toc() == before.get_toc()
        for left, right in zip(before, after, strict=True):
            assert left.get_text() == right.get_text()
            assert left.get_pixmap().samples == right.get_pixmap().samples


def test_attachment_inventory_and_generated_names(tmp_path, source):
    inspect = run(tmp_path, "inspectpdf", source)
    assert inspect.state == "completed", inspect.errors
    report = json.loads(result(inspect, ".json").read_text())
    assert report["has_outlines"] is True
    assert report["attachments"][0]["name"] == "../unsafe.wav"
    job = run(tmp_path, "attachments", source)
    assert job.state == "completed", job.errors
    content = result(job, ".bin").read_bytes()
    assert content == b"synthetic media bytes"
    manifest = json.loads(result(job, ".json").read_text())["attachments"][0]
    assert manifest["sha256"] == hashlib.sha256(content).hexdigest()
    assert not (tmp_path / "unsafe.wav").exists()


@pytest.mark.parametrize("renderer", ["pymupdf", "ghostscript"])
def test_render_engines_honor_page_selection_dpi(tmp_path, source, renderer):
    job = run(tmp_path, "render_native", source, {"renderer": renderer, "dpi": 144, "pages": "2"})
    assert job.state == "completed", job.errors
    pix = pymupdf.Pixmap(result(job, ".png"))
    assert (pix.width, pix.height) == (800, 800)
    assert "0002" in job.results[0]["name"]


def test_real_tesseract_searchable_pdf_text_and_confidence(tmp_path):
    source = scan_fixture(tmp_path / "scan.pdf")
    job = run(tmp_path, "ocr_tesseract", source, {"allow_rasterize": True})
    assert job.state == "completed", job.errors
    assert "Invoice 12345" in result(job, ".txt").read_text()
    assert "conf" in result(job, ".tsv").read_text().splitlines()[0]
    with pymupdf.open(source) as before, pymupdf.open(result(job, ".pdf")) as after:
        assert before[0].get_text() == ""
        assert "Invoice 12345" in after[0].get_text()
        assert abs(before[0].rect.width - after[0].rect.width) < 2


def test_deskew_detects_small_rotation_and_reports(tmp_path):
    source = scan_fixture(tmp_path / "skew.pdf", angle=4)
    job = run(tmp_path, "deskew", source, {"allow_rasterize": True})
    assert job.state == "completed", job.errors
    report = json.loads(result(job, ".json").read_text())
    assert abs(report["pages"][0]["angle"] + 4) < 0.7
    with pymupdf.open(result(job, ".pdf")) as doc:
        assert len(doc) == 1
        assert len(doc[0].get_images()) == 1


def test_no_tables_or_no_ocr_text_is_partial(tmp_path):
    blank = tmp_path / "blank.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(blank)
    for operation, options in (
        ("tables", {}),
        ("attachments", {}),
        ("ocr_tesseract", {"allow_rasterize": True}),
        ("deskew", {"allow_rasterize": True}),
    ):
        job = run(tmp_path, operation, blank, options)
        assert job.state == "partial", job.snapshot()
        assert job.results


MALFORMED_OPTIONS = {
    "oxide_edit_text": {"action": "add", "text": "Fixture"},
    "oxide_edit_images": {"changes": '[{"page":1,"action":"clear"}]'},
    "oxide_edit_embed": {"attachments": '[{"name":"fixture.txt","data_base64":"eA=="}]'},
    "oxide_form_fill": {"values": '{"fixture":"001"}'},
    "oxide_form_flatten": {"allow_flatten": True},
    "oxide_annotation_edit": {
        "actions": '[{"type":"note","page":1,"x":20,"y":20,"text":"Fixture"}]'
    },
    "oxide_annotation_flatten": {"allow_flatten": True},
    "oxide_flatten": {"allow_rasterization": True, "allow_page_relayout": True},
    "oxide_redact": {"regions": '[{"page":1,"rect":[10,10,20,20]}]'},
    "oxide_extract_mask": {"actions_json": '[{"action":"clear","page":1}]'},
    "oxide_search": {"pattern": "Fixture"},
}


@pytest.mark.parametrize(
    "operation", [name for name, contract in OPERATIONS.items() if contract["inputs"] > 0]
)
def test_malformed_input_never_publishes_part(tmp_path, operation):
    # Supply a valid operation contract so failure cannot be explained by missing
    # new-route options or by sending an input to a zero-input creation operation.
    options = MALFORMED_OPTIONS.get(operation, {})
    validate_options(operation, options)
    suffix = ".docx" if operation == "oxide_office_import" else ".pdf"
    if operation in {"oxide_image_pdf", "oxide_images_pdf"}:
        suffix = ".png"
    files = []
    malformed = b"%PDF-1.7\n malformed synthetic data"
    for index in range(OPERATIONS[operation]["inputs"]):
        source = tmp_path / f"malformed-{index}{suffix}"
        source.write_bytes(malformed)
        files.append({"id": str(index), "name": source.name, "path": str(source)})
    job = Job(operation, files, options, tmp_path / "jobs")
    job.run()
    assert job.state == "failed", job.snapshot()
    assert job.results == []
    assert list(job.directory.iterdir()) == []
    assert all(Path(file["path"]).read_bytes() == malformed for file in files)


@pytest.mark.parametrize(
    "operation, options",
    [
        ("create", {"font_size": True}),
        ("paginate", {"margin": float("nan")}),
        ("tables", {"strategy": "guess"}),
        ("optimize", {"compression_level": 10}),
        ("geometry", {"dedupe_chars": "yes"}),
        ("inspectpdf", {"url": "https://example.com"}),
        ("ocr_tesseract", {"psm": 999}),
        ("deskew", {"angle": 100}),
        ("render_native", {"renderer": "sh"}),
        ("deduplicate", {"command": "anything"}),
    ],
)
def test_invalid_options_rejected_before_worker(tmp_path, source, operation, options):
    with pytest.raises(JobError):
        run(tmp_path, operation, None if operation == "create" else source, options)
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize(
    "operation, options",
    [
        ("ocr_tesseract", {}),
        ("deskew", {}),
        ("ocr_tesseract", {"allow_rasterize": True, "language": "../../outside"}),
        ("ocr_tesseract", {"allow_rasterize": True, "language": "uninstalled_fixture_language"}),
        ("paginate", {"header": "a" * 190}),
        ("render_native", {"pages": "999"}),
    ],
)
def test_failure_paths_clean_up_and_preserve_original(tmp_path, source, operation, options):
    job = run(tmp_path, operation, source, options)
    assert job.state == "failed", job.snapshot()
    assert not job.results
    assert list(job.directory.iterdir()) == []


def test_new_workflow_obeys_worker_output_quota(tmp_path, source):
    job = run(tmp_path, "geometry", source, limits=replace(Limits(), max_output_bytes=1000))
    assert job.state == "failed", job.snapshot()
    assert not job.results
    assert list(job.directory.iterdir()) == []


def test_partial_batch_keeps_only_successful_original(tmp_path, source):
    invalid = tmp_path / "invalid.pdf"
    invalid.write_bytes(b"bad")
    job = Job(
        "optimize",
        [{"id": str(n), "name": p.name, "path": str(p)} for n, p in enumerate((source, invalid))],
        {},
        tmp_path / "jobs",
    )
    job.run()
    assert job.state == "partial", job.snapshot()
    assert len(job.results) == 2
    assert len(job.errors) == 1
    assert not (job.directory / "part-0002").exists()
