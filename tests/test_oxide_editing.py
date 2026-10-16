"""Real supervised editing, independent reopens and rejected edit plans."""

import base64
import hashlib
import json
from dataclasses import replace

import pymupdf
import pytest

from pdf_multitool import oxide, oxide_editing
from pdf_multitool.catalog import validate_options
from pdf_multitool.core import Job, JobError, Limits
from tests.test_extended import result, run

NATIVE = pytest.mark.skipif(not oxide.availability()["available"], reason="Optional bounded Oxide")


def source_pdf(tmp_path, *, image=False, bookmarks=False):
    source = tmp_path / "editing-source.pdf"
    with pymupdf.open() as document:
        for number in range(1, 4):
            page = document.new_page(width=300, height=400)
            page.insert_text((40, 70), f"Invoice {number:03d} Total {number}.25")
            page.insert_text((40, 100), f"Retain {number * 10}")
            if image and number == 1:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10), False)
                pix.clear_with(180)
                page.insert_image(pymupdf.Rect(50, 120, 90, 160), stream=pix.tobytes("png"))
        document.set_metadata({"title": "Original invoice", "author": "Public fixture"})
        if bookmarks:
            document.set_toc([[1, "../Chapter first", 1], [1, "Chapter second", 3]])
        document.save(source)
    return source


def manifest(job):
    return json.loads(result(job, "oxide-editing-manifest.json").read_text())


def assert_completed(job):
    assert job.state == "completed", job.errors
    report = manifest(job)
    assert report["semantics_verified"] is False
    assert report["visual_review"] == "pending"
    assert report["truncated"] is False
    return report


@pytest.mark.parametrize(
    "operation,options",
    [
        ("oxide_edit_ranges", {"ranges": "[[1,999999999999]]"}),
        ("oxide_edit_ranges", {"ranges": "[[1,2],[2,3]]"}),
        ("oxide_edit_ranges", {"ranges": "[[true,2]]"}),
        ("oxide_edit_geometry", {"box": "[0,0,0,1]"}),
        ("oxide_edit_geometry", {"box": "[0,0,NaN,1]"}),
        ("oxide_edit_geometry", {"margins": "[1,-1,1,1]"}),
        ("oxide_edit_metadata", {"metadata": '{"title":"A","title":"B"}'}),
        ("oxide_edit_metadata", {"metadata": '{"path":"/private/file"}'}),
        ("oxide_edit_metadata", {"metadata": '{"title":false}'}),
        ("oxide_edit_text", {"find": ""}),
        ("oxide_edit_images", {"changes": '[{"page":1,"image":"Im1","action":"call"}]'}),
        (
            "oxide_edit_images",
            {"changes": '[{"page":1,"image":"Im1","action":"move","x":0,"y":true}]'},
        ),
        ("oxide_edit_embed", {"attachments": '[{"name":"../private","data_base64":"YQ=="}]'}),
        ("oxide_edit_embed", {"attachments": '[{"name":"a","data_base64":"???"}]'}),
    ],
)
def test_reject_invalid_edit_plans_before_job_directory(tmp_path, operation, options):
    with pytest.raises(JobError):
        Job(operation, [{"id": "x"}], options, tmp_path / "jobs")
    assert not (tmp_path / "jobs").exists()


def test_attachment_total_budget_before_spawn():
    data = base64.b64encode(b"x" * (64 * 1024 + 1)).decode("ascii")
    with pytest.raises(JobError, match="64 KiB"):
        oxide_editing.validate_options(
            "oxide_edit_embed", {"attachments": json.dumps([{"name": "a", "data_base64": data}])}
        )


@NATIVE
def test_structure_report_preserves_original_text_and_hash(tmp_path):
    source = source_pdf(tmp_path, image=True, bookmarks=True)
    job = run(tmp_path, "oxide_edit_inspect", source, {"pages": "1"})
    report = assert_completed(job)
    assert report["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report["before"]["structure"]["pages"] == 3
    page = report["before"]["page_snapshots"][0]
    assert "001 Total 1.25" in page["text"] and "Retain 10" in page["text"]
    assert page["text_elements"] and len(page["images"]) == 1
    assert report["before"]["structure"]["outlines"][0]["title"] == "../Chapter first"
    assert not report["outputs"]


@NATIVE
@pytest.mark.parametrize(
    "options,order",
    [
        ({"action": "extract", "pages": "3,1"}, [3, 1]),
        ({"action": "select", "pages": "3,1"}, [3, 1]),
        ({"action": "delete", "pages": "2"}, [1, 3]),
        ({"action": "move", "pages": "1", "destination": 3}, [2, 3, 1]),
    ],
)
def test_page_operations_preserve_requested_order_and_numbers(tmp_path, options, order):
    job = run(tmp_path, "oxide_edit_pages", source_pdf(tmp_path), options)
    assert_completed(job)
    with pymupdf.open(result(job, "edited.pdf")) as document:
        assert len(document) == len(order)
        for page, number in zip(document, order, strict=True):
            assert f"Invoice {number:03d} Total {number}.25" in page.get_text()
            assert f"Retain {number * 10}" in page.get_text()


@NATIVE
def test_range_exports_and_bookmark_plan_and_bytes(tmp_path):
    source = source_pdf(tmp_path, bookmarks=True)
    job = run(tmp_path, "oxide_edit_ranges", source, {"ranges": "[[1,2],[3,3]]"})
    report = assert_completed(job)
    assert [output["pages"] for output in report["outputs"]] == [2, 1]
    for mode in ("plan", "split"):
        job = run(tmp_path, "oxide_edit_bookmarks", source, {"mode": mode})
        report = assert_completed(job)
        assert [(item["start_page"], item["end_page"]) for item in report["bookmark_plan"]] == [
            (0, 2),
            (2, 3),
        ]
        assert len(report["outputs"]) == (2 if mode == "split" else 0)
        if mode == "split":
            assert [item["file"] for item in report["outputs"]] == [
                "bookmark-0001.pdf",
                "bookmark-0002.pdf",
            ]


@NATIVE
@pytest.mark.parametrize(
    "options,rotation,box",
    [
        ({"action": "rotate", "degrees": 90, "pages": "1"}, 90, None),
        ({"action": "rotate", "degrees": -90, "pages": "1"}, 270, None),
        ({"action": "rotate", "degrees": 180}, 180, None),
        ({"action": "set_rotation", "degrees": -90, "pages": "1"}, 270, None),
        ({"action": "crop_box", "box": "[10,20,290,380]", "pages": "1"}, 0, [10, 20, 290, 380]),
        ({"action": "media_box", "box": "[0,0,310,410]", "pages": "1"}, 0, None),
        ({"action": "crop_margins", "margins": "[10,10,20,20]"}, 0, [10, 20, 290, 380]),
    ],
)
def test_page_geometry_roundtrip(tmp_path, options, rotation, box):
    job = run(tmp_path, "oxide_edit_geometry", source_pdf(tmp_path), options)
    report = assert_completed(job)
    with pymupdf.open(result(job, "edited.pdf")) as document:
        assert document[0].rotation == rotation
        assert "001" in document[0].get_text()
    output_page = report["outputs"][0]["page_snapshots"][0]
    if box:
        assert output_page["crop_box"] == box
    if options["action"] == "media_box":
        assert output_page["media_box"] == [0, 0, 310, 410]


@NATIVE
def test_metadata_changes_and_native_text_copies(tmp_path):
    changes = {
        "title": "Revised 001",
        "author": "Public tests",
        "subject": "Total 12.50",
        "keywords": "fixture",
    }
    job = run(
        tmp_path, "oxide_edit_metadata", source_pdf(tmp_path), {"metadata": json.dumps(changes)}
    )
    report = assert_completed(job)
    with pymupdf.open(result(job, "edited.pdf")) as document:
        assert {key: document.metadata[key] for key in changes} == changes
    assert (
        report["before"]["page_snapshots"][0]["text"]
        == report["outputs"][0]["page_snapshots"][0]["text"]
    )


@NATIVE
def test_added_text_has_independently_visible_content(tmp_path):
    source = source_pdf(tmp_path)
    options = {
        "action": "add",
        "pages": "1",
        "find": "Invoice",
        "text": "Receipt",
        "x": 40,
        "y": 200,
    }
    job = run(tmp_path, "oxide_edit_text", source, options)
    report = assert_completed(job)
    assert report["text_changes"]
    with pymupdf.open(result(job, "edited.pdf")) as document:
        text = document[0].get_text()
        assert "Retain 10" in text
        assert "Invoice 002 Total 2.25" in document[1].get_text()
        assert "Receipt" in text and "Invoice 001 Total 1.25" in text


@pytest.mark.parametrize("action", ["replace", "remove"])
def test_known_black_mask_routes_are_rejected_before_job_directory(tmp_path, action):
    with pytest.raises(JobError, match="black mask"):
        Job(
            "oxide_edit_text",
            [{"id": "synthetic"}],
            {"action": action, "find": "Invoice", "text": "Receipt"},
            tmp_path / "jobs",
        )
    assert not (tmp_path / "jobs").exists()


@NATIVE
@pytest.mark.parametrize("action", ["move", "resize", "bounds"])
def test_image_placement_preserves_encoded_image_and_changes_bounds(tmp_path, action):
    source = source_pdf(tmp_path, image=True)
    inspect = run(tmp_path, "oxide_edit_inspect", source, {"pages": "1"})
    image = assert_completed(inspect)["before"]["page_snapshots"][0]["images"][0]
    item = {"page": 1, "image": image["name"], "action": action}
    if action in {"move", "bounds"}:
        item.update(x=70, y=200)
    if action in {"resize", "bounds"}:
        item.update(width=60, height=50)
    job = run(tmp_path, "oxide_edit_images", source, {"changes": json.dumps([item])})
    report = assert_completed(job)
    after = report["outputs"][0]["page_snapshots"][0]["images"][0]
    for key in ("x", "y", "width", "height"):
        assert after[key] == pytest.approx(item.get(key, image[key]))
    with pymupdf.open(source) as original, pymupdf.open(result(job, "edited.pdf")) as edited:
        first = original.extract_image(original[0].get_images()[0][0])["image"]
        second = edited.extract_image(edited[0].get_images()[0][0])["image"]
        assert first == second


@NATIVE
def test_attachment_bytes_roundtrip(tmp_path):
    payload = b"public fixture\n001,12.50\n"
    attachments = [{"name": "data.csv", "data_base64": base64.b64encode(payload).decode("ascii")}]
    job = run(
        tmp_path, "oxide_edit_embed", source_pdf(tmp_path), {"attachments": json.dumps(attachments)}
    )
    report = assert_completed(job)
    assert report["embedded_files"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    with pymupdf.open(result(job, "edited.pdf")) as document:
        assert document.embfile_get("data.csv") == payload


@NATIVE
def test_pending_image_edits_can_be_cleared_within_one_request(tmp_path):
    source = source_pdf(tmp_path, image=True)
    inspect = run(tmp_path, "oxide_edit_inspect", source, {"pages": "1"})
    before = assert_completed(inspect)["before"]["page_snapshots"][0]["images"][0]
    changes = [
        {"page": 1, "image": before["name"], "action": "move", "x": 100, "y": 200},
        {"page": 1, "action": "clear"},
    ]
    job = run(tmp_path, "oxide_edit_images", source, {"changes": json.dumps(changes)})
    report = assert_completed(job)
    assert report["image_changes"][-1]["pending_modifications"] is False
    assert report["outputs"][0]["page_snapshots"][0]["images"][0] == before


@NATIVE
def test_missing_added_glyphs_fail_without_publishing(tmp_path):
    job = run(
        tmp_path,
        "oxide_edit_text",
        source_pdf(tmp_path),
        {"action": "add", "pages": "1", "text": "Amount 001 café 日本語"},
    )
    assert job.state == "failed", job.snapshot()
    assert "glyphs/text" in job.errors[0]["error"]
    assert not job.results and list(job.directory.iterdir()) == []


@NATIVE
def test_authenticated_encrypted_editing_gap_is_not_reported_as_success(tmp_path):
    source = source_pdf(tmp_path)
    encrypted = tmp_path / "encrypted.pdf"
    with pymupdf.open(source) as document:
        document.save(
            encrypted,
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            user_pw="synthetic-password",
            owner_pw="synthetic-owner",
        )
    job = run(
        tmp_path,
        "oxide_edit_metadata",
        encrypted,
        {"password": "synthetic-password", "metadata": '{"title":"After"}'},
    )
    assert job.state == "failed" and not job.results
    assert list(job.directory.iterdir()) == []
    assert "synthetic-password" not in json.dumps(job.snapshot())


@NATIVE
def test_merge_source_order_and_bytes_preserved(tmp_path):
    first = source_pdf(tmp_path)
    second = tmp_path / "other.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((40, 70), "Fourth 004 Total -0.15")
        document.save(second)
    originals = [path.read_bytes() for path in (first, second)]
    job = Job(
        "oxide_edit_merge",
        [
            {"id": str(index), "name": path.name, "path": path}
            for index, path in enumerate((first, second))
        ],
        {},
        tmp_path / "merge-jobs",
    )
    job.run()
    report = assert_completed(job)
    assert len(report["sources"]) == 2
    with pymupdf.open(result(job, "edited.pdf")) as document:
        assert len(document) == 4
        assert "Invoice 001 Total 1.25" in document[0].get_text()
        assert "Fourth 004 Total -0.15" in document[3].get_text()
    assert [path.read_bytes() for path in (first, second)] == originals


@NATIVE
@pytest.mark.parametrize(
    "operation,options",
    [
        ("oxide_edit_pages", {"action": "delete"}),
        ("oxide_edit_pages", {"action": "move", "pages": "1,2"}),
        ("oxide_edit_pages", {"action": "move", "pages": "1", "destination": 10}),
        ("oxide_edit_ranges", {"ranges": "[[1,4]]"}),
        ("oxide_edit_geometry", {"action": "crop_margins", "pages": "1"}),
        ("oxide_edit_geometry", {"action": "crop_margins", "margins": "[200,200,0,0]"}),
        ("oxide_edit_bookmarks", {"mode": "split"}),
        (
            "oxide_edit_images",
            {"changes": '[{"page":1,"image":"missing","action":"move","x":0,"y":0}]'},
        ),
    ],
)
def test_document_dependent_failure_discards_outputs(tmp_path, operation, options):
    job = run(tmp_path, operation, source_pdf(tmp_path), options)
    assert job.state == "failed", job.snapshot()
    assert not job.results and list(job.directory.iterdir()) == []


@NATIVE
def test_output_overflow_does_not_publish_derivative(tmp_path):
    job = run(
        tmp_path,
        "oxide_edit_geometry",
        source_pdf(tmp_path),
        limits=replace(Limits(), max_output_bytes=64),
    )
    assert job.state == "failed" and not job.results
    assert list(job.directory.iterdir()) == []


def test_all_editing_contracts_validate_defaults_when_action_requires_no_data():
    for name in (
        "oxide_edit_inspect",
        "oxide_edit_pages",
        "oxide_edit_ranges",
        "oxide_edit_geometry",
        "oxide_edit_metadata",
        "oxide_edit_bookmarks",
        "oxide_edit_merge",
    ):
        assert isinstance(validate_options(name, {}), dict)
