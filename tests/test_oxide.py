"""Pinned native adapter: exact cells, honest failures and bounded worker behavior."""

import hashlib
import json
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

import pymupdf
import pytest

from pdf_multitool import oxide
from pdf_multitool.catalog import validate_options
from pdf_multitool.core import Job, JobError, Limits, engines
from tests.test_extended import result, run, scan_fixture

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "experiments"
NATIVE = pytest.mark.skipif(not oxide.availability()["available"], reason="Optional bounded Oxide")


def fixture(group, name):
    return FROZEN / group / "fixtures" / f"{name}.pdf"


def page_result(job, number=1):
    return json.loads(result(job, f"page-{number:04d}.json").read_text())


def cells(page):
    return [
        [[cell["text"] for cell in row["cells"]] for row in table["rows"]]
        for table in page["tables"]
    ]


@pytest.mark.parametrize("pages", ["0", "2001", "1-999999999999999", "1-101", "1--2", "2-1", "x"])
def test_invalid_page_selection_rejected_before_job_directory(tmp_path, pages):
    with pytest.raises(JobError):
        Job("oxide", [{"id": "x"}], {"pages": pages}, tmp_path / "jobs")
    assert not (tmp_path / "jobs").exists()


def test_cleanup_options_fail_closed():
    for options in (
        {"cleanup": "both", "mode": "tables"},
        {"threshold": 0},
        {"strategy": "explicit"},
        {"ocr": True},
    ):
        with pytest.raises(JobError):
            validate_options("oxide", options)
    assert validate_options("oxide", {})["cleanup"] == "none"


def test_no_unsupervised_parser_import(monkeypatch):
    monkeypatch.setattr(oxide, "_guard", None)
    with pytest.raises(JobError, match="bounded document worker"):
        oxide.extract({}, {}, None)
    monkeypatch.setattr(oxide.sys, "platform", "win32")
    with pytest.raises(JobError, match="confinement"):
        oxide.initialize_guard({}, asdict(Limits()))


@NATIVE
@pytest.mark.parametrize(
    "group,name,expected",
    [
        ("representation_v1", "table_column_operators", [["A1", "B1"], ["A2", "B2"], ["A3", "B3"]]),
        (
            "structured_cells_v1",
            "missing_borderless_reverse",
            [["Key", "Amount", "Tax"], ["012", "", "0.15"], ["036", "2.00", ""]],
        ),
    ],
)
def test_direct_cells_preserve_missing_values_and_numbers(tmp_path, group, name, expected):
    source = fixture(group, name)
    job = run(tmp_path, "oxide", source)
    assert job.state == "completed", job.errors
    page = page_result(job)
    assert cells(page) == [expected]
    assert page["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert page["native_page_index"] == 0 and page["page"] == 1
    assert result(job, "page-0001.txt").read_text() == page["text"]
    manifest = json.loads(result(job, "oxide-manifest.json").read_text())
    assert manifest["semantics_verified"] is False
    assert manifest["bounds"]["kernel_limits"]["RLIMIT_AS"]["hard"] <= 2 * 1024**3
    assert manifest["bounds"]["native_network_filter"] is True
    assert manifest["bounds"]["filesystem_isolation"] is False


@NATIVE
@pytest.mark.parametrize(
    "name", ["wrapped_ruled_reverse", "wrapped_borderless_reverse", "empty_row_ruled_reverse"]
)
def test_known_native_table_failures_are_retained_not_repaired(tmp_path, name):
    contracts = json.loads((FROZEN / "structured_cells_v1/contracts.json").read_text())
    job = run(tmp_path, "oxide", fixture("structured_cells_v1", name))
    assert job.state == "completed", job.errors
    page = page_result(job)
    assert cells(page) != [contracts[name]["authored_cells"]]
    assert "correctness" in " ".join(job.warnings)
    if name.startswith("wrapped"):
        assert cells(page)[0][2] == ["", "continued description", ""]
    else:
        assert cells(page) == [[["001", "0.00"], ["002", "-3.25"]]]


@NATIVE
def test_numeric_prose_not_reported_as_cells(tmp_path):
    job = run(tmp_path, "oxide", fixture("structured_cells_v1", "wrapped_numeric_prose_reverse"))
    assert job.state == "completed", job.errors
    assert cells(page_result(job)) == []
    assert page_result(job)["table_status"] == "none_detected"


@NATIVE
def test_blank_and_scan_keep_explicit_empty_status_without_fake_ocr(tmp_path):
    blank = tmp_path / "blank.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(blank)
    for source in (blank, scan_fixture(tmp_path / "scan.pdf")):
        job = run(tmp_path, "oxide", source)
        assert job.state in {"completed", "partial"}, job.errors
        page = page_result(job)
        assert page["text"] == "" and page["text_status"] == "empty"
        assert page["ocr"] == "not_run"
        manifest = json.loads(result(job, "oxide-manifest.json").read_text())
        if page["ocr_required"] is True:
            assert any(
                w["native"]["category"] == "no_text_layer" for w in manifest["structured_warnings"]
            )
        else:
            assert page["ocr_required"] is None


@NATIVE
def test_encryption_wrong_password_no_argv_or_report_leak(tmp_path, monkeypatch):
    source = tmp_path / "encrypted.pdf"
    password = "synthetic-fixture-password"  # noqa: S105 -- generated public test fixture
    with pymupdf.open() as doc:
        doc.new_page().insert_text((70, 200), "Invoice 001 Total 12.50")
        doc.save(
            source, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw="owner-test"
        )
    spawn = subprocess.Popen
    captured = []

    def observe(args, **kwargs):
        captured.append(args)
        return spawn(args, **kwargs)

    monkeypatch.setattr("pdf_multitool.core.subprocess.Popen", observe)
    for attempt in ("", "incorrect", password):
        job = run(tmp_path, "oxide", source, {"password": attempt})
        assert "password" not in job.options
        if attempt == password:
            assert job.state == "completed", job.errors
            assert "001 Total 12.50" in page_result(job)["text"]
        else:
            assert job.state == "failed" and not job.results
            assert list(job.directory.iterdir()) == []
        assert password not in json.dumps(job.snapshot())
    assert password not in repr(captured)


@NATIVE
def test_input_output_and_wall_limits_discard_every_unpublished_file(tmp_path):
    source = fixture("representation_v1", "table_column_operators")
    for limits in (
        replace(Limits(), max_file_bytes=32),
        replace(Limits(), max_output_bytes=64),
        replace(Limits(), timeout_seconds=0),
        replace(Limits(), memory_mb=4096),  # Adapter requires <=2GiB; cannot silently widen it.
    ):
        job = run(tmp_path, "oxide", source, limits=limits)
        assert job.state == "failed", job.snapshot()
        assert not job.results and list(job.directory.iterdir()) == []


@NATIVE
def test_failed_file_does_not_abort_batch_and_can_be_retried(tmp_path):
    bad = tmp_path / "empty.pdf"
    bad.write_bytes(b"")
    good = fixture("representation_v1", "table_column_operators")
    files = [
        {"id": str(i), "path": str(path), "name": path.name} for i, path in enumerate((bad, good))
    ]
    job = Job("oxide", files, {}, tmp_path / "jobs")
    job.run()
    assert job.state == "partial" and len(job.errors) == 1 and len(job.results) == 4
    assert not (job.directory / "part-0001").exists()
    retry = run(tmp_path, "oxide", good)
    assert retry.state == "completed"
    assert cells(page_result(retry)) == cells(page_result(job))


def cleanup_fixture(path, tagged=False):
    with pymupdf.open() as doc:
        for i in range(2):
            page = doc.new_page(width=600, height=800)
            page.insert_text((60, 40), "Meaningful repeated table heading 2026")
            if tagged and i == 0:
                xref = page.get_contents()[0]
                doc.update_stream(
                    xref,
                    b"/Artifact <</Type /Pagination /Subtype /Header>> BDC\n"
                    + doc.xref_stream(xref)
                    + b"\nEMC",
                )
            page.insert_text((60, 300), "Meaningful repeated table heading 2026")
            page.insert_text((60, 330), f"Body record {i + 1} 012 0.15")
            page.insert_text((60, 770), "Citation 7: repeated supporting reference")
        doc.save(path)
    return path


@NATIVE
@pytest.mark.parametrize("tagged", [False, True])
def test_cleanup_false_positives_visible_and_original_copy_retained(tmp_path, tagged):
    source = cleanup_fixture(tmp_path / "cleanup.pdf", tagged)
    original = run(tmp_path, "oxide", source)
    cleaned = run(tmp_path, "oxide", source, {"cleanup": "both"})
    assert original.state == cleaned.state == "completed", cleaned.errors
    changed = []
    for number in (1, 2):
        before, after = page_result(original, number), page_result(cleaned, number)
        assert before["text"] == after["text"]
        assert before["tables"] == after["tables"]
        assert before["text"].count("Meaningful repeated table heading 2026") == 2
        assert "Citation 7" in before["text"]
        assert "Meaningful repeated table heading 2026" in after["cleanup"]["text"]
        assert "Body record" in after["cleanup"]["text"]
        changed.append(after["cleanup"]["changed"])
    assert any(changed), "Fixture must expose an actual cleanup transformation"
    assert "meaningful content" in " ".join(cleaned.warnings)
    again = run(tmp_path, "oxide", source)
    assert page_result(again)["text"] == page_result(original)["text"]


@NATIVE
def test_cancel_real_native_output_removes_partial_and_reaps_descendants(tmp_path, monkeypatch):
    source = fixture("representation_v1", "table_column_operators")
    job = Job(
        "oxide", [{"id": "f", "name": source.name, "path": str(source)}], {}, tmp_path / "jobs"
    )
    spawn = subprocess.Popen
    children = []
    code = """
import os, time
from pathlib import Path
from pdf_multitool import worker
original = worker.process
def held(request):
    result = original(request)
    child = os.fork()
    if child == 0:
        time.sleep(30)
        os._exit(0)
    Path(request['workdir'], 'descendant.pid').write_text(str(child))
    time.sleep(30)
    return result
worker.process = held
worker.main()
"""

    def held(args, **kwargs):
        child = spawn([sys.executable, "-c", code], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("pdf_multitool.core.subprocess.Popen", held)
    thread = threading.Thread(target=job.run)
    thread.start()
    pid = None
    try:
        deadline = time.monotonic() + 10
        ready = job.directory / "part-0001/descendant.pid"
        while not ready.exists() and thread.is_alive() and time.monotonic() < deadline:
            thread.join(0.02)
        assert ready.exists(), job.snapshot()
        pid = int(ready.read_text())
        assert (ready.parent / "page-0001.json").exists()
        assert not job.results
    finally:
        job.cancel_event.set()
        thread.join(8)
    assert not thread.is_alive() and job.state == "cancelled"
    assert children[0].poll() is not None
    status = Path(f"/proc/{pid}/status")
    deadline = time.monotonic() + 3
    while status.exists() and "State:\tZ" not in status.read_text() and time.monotonic() < deadline:
        threading.Event().wait(0.02)
    assert not status.exists() or "State:\tZ" in status.read_text()
    assert not job.results and not list(job.directory.iterdir())


def test_optional_engine_metadata_does_not_import_parser(monkeypatch):
    monkeypatch.setattr(oxide.importlib.metadata, "version", lambda name: "0.0.0")
    assert engines()["pdf_oxide"]["available"] is False


def test_html_active_content_and_external_resources_are_removed():
    from pdf_multitool.oxide_html import sanitize

    hostile = (
        '<script>alert(1)</script><p onclick="alert(2)" style="background:url(https://invalid)">'
        '<a href="javascript:alert(3)">Invoice 001</a>'
        '<img src="https://invalid" onerror="alert(4)">'
        '<svg><script>alert(5)</script></svg><iframe src="file:///private">hidden</iframe>'
        "&lt;img src=x&gt; Total 12.50</p>"
    )
    parsed = sanitize(hostile)
    assert parsed["fragment"] == "<p>Invoice 001&lt;img src=x&gt; Total 12.50</p>"
    assert parsed["removed_tags"] > 0 and parsed["removed_attributes"] > 0
    with pytest.raises(JobError, match="nesting"):
        sanitize("<div>" * 65)


@NATIVE
def test_page_html_preserves_raw_string_separately_and_source_identity(tmp_path):
    source = fixture("representation_v1", "table_column_operators")
    job = run(tmp_path, "oxide", source, {"include_html": True})
    assert job.state == "completed", job.errors
    page = page_result(job)
    assert page["html"]["native"] == result(job, ".native-html.txt").read_text()
    assert "A1" in page["html"]["native"] and "B3" in page["html"]["native"]
    review = result(job, ".html").read_text()
    assert f"source-{page['source_sha256']}-page-1" in review
    assert "<script" not in review and "<img" not in review and " href=" not in review
    assert cells(page) == [[["A1", "B1"], ["A2", "B2"], ["A3", "B3"]]]


@NATIVE
def test_image_bytes_inspection_is_memory_only_export_is_explicit(tmp_path):
    from PIL import Image

    source = scan_fixture(tmp_path / "scan.pdf")
    inspected = run(tmp_path, "oxide", source, {"images": "inspect"})
    exported = run(tmp_path, "oxide", source, {"images": "export"})
    for job in (inspected, exported):
        assert job.state in {"completed", "partial"}, job.errors
        page = page_result(job)
        assert len(page["images"]) == 1
        item = page["images"][0]
        assert item["page"] == 1 and item["image_index"] == 1
        assert item["native_metadata"]["width"] == 1500
        assert item["native_metadata"]["height"] == 800
        assert item["bytes"] > 0 and len(item["sha256"]) == 64
        assert page["image_completeness_verified"] is False
    assert not list(inspected.directory.rglob("*.png"))
    assert page_result(inspected)["images"][0]["persisted"] is False
    png = result(exported, ".png")
    assert (
        hashlib.sha256(png.read_bytes()).hexdigest()
        == page_result(inspected)["images"][0]["sha256"]
    )
    with Image.open(png) as image:
        image.load()
        assert image.size == (1500, 800)


def test_image_limits_precede_png_conversion_and_reject_mismatch(tmp_path):
    class FakeNative:
        def extract_images(self, index):
            return [{"width": 6000, "height": 6000}]

        def extract_image_bytes(self, index):
            pytest.fail("Oversized decoded image reached PNG conversion")

    with pytest.raises(JobError, match="pixel limit"):
        oxide._images(FakeNative(), 0, "f" * 64, oxide._Budget(100000), tmp_path, "inspect", None)
