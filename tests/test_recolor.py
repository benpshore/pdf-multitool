"""PY-036 synthetic truth through real workers and independent PDF structure checks."""

import hashlib
import io
import json
from dataclasses import replace

import pikepdf
import pymupdf
import pytest
from PIL import Image

from pdf_multitool.cli import main
from pdf_multitool.core import JobError, Limits
from tests.test_extended import result, run
from tests.test_svg import svg_fixture


def recolor_fixture(path):
    base = path.with_name("color-base.pdf")
    svg_fixture(base)
    alpha = io.BytesIO()
    Image.new("RGBA", (10, 10), (255, 0, 0, 128)).save(alpha, format="PNG")
    with pymupdf.open(base) as doc:
        doc[0].insert_image((110, 140, 130, 160), stream=alpha.getvalue())
        doc.set_metadata({"title": "Synthetic recolor truth"})
        doc.save(path, deflate=True)
    return path


@pytest.fixture
def source(tmp_path):
    return recolor_fixture(tmp_path / "color-source.pdf")


def image_components(image):
    cs = image.ColorSpace
    if isinstance(cs, pikepdf.Array) and cs[0] == pikepdf.Name.ICCBased:
        return int(cs[1].N)
    return {"/DeviceGray": 1, "/DeviceRGB": 3, "/DeviceCMYK": 4}[str(cs)]


@pytest.mark.parametrize("colorspace,components", [("gray", 1), ("rgb", 3), ("cmyk", 4)])
def test_recolor_structure_text_geometry_images_and_manifest(
    tmp_path, source, colorspace, components
):
    job = run(tmp_path, "recolor", source, {"colorspace": colorspace, "allow_color_change": True})
    assert job.state == "completed", job.snapshot()
    output = result(job, ".pdf")
    report = json.loads(result(job, ".json").read_text())
    assert report["feature_id"] == "PY-036"
    assert report["color_components"] == components
    assert report["visual_review"] == "not_reviewed"
    assert report["output"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert report["output"]["file"] in [item["name"] for item in job.results]
    assert report["visible_image_instances_checked"] == 3
    with pymupdf.open(source) as original, pymupdf.open(output) as converted:
        assert len(converted) == 3
        assert converted.metadata["title"] == original.metadata["title"]
        for before, after in zip(original, converted, strict=True):
            assert before.get_text() == after.get_text()
            assert before.cropbox == after.cropbox
            assert before.mediabox == after.mediabox
            assert before.rotation == after.rotation
    with pikepdf.open(output) as pdf:
        for page in list(pdf.pages)[:2]:
            operators = {str(item.operator) for item in pikepdf.parse_content_stream(page)}
            expected = {1: "g", 3: "rg", 4: "k"}[components]
            assert expected in operators
            assert not ({"g", "G", "rg", "RG", "k", "K"} - {expected, expected.upper()}) & operators
            for image in page.get_images().values():
                assert image_components(image) == components
                if "/SMask" in image:
                    assert image_components(image.SMask) == 1


def test_recolor_cli_requires_acknowledgment_then_recovers(tmp_path, source, capsys):
    failed = run(tmp_path, "recolor", source)
    assert failed.state == "failed" and not failed.results
    assert not list(failed.directory.iterdir())
    out = tmp_path / "cli"
    assert (
        main(
            ["recolor", str(source), "--colorspace", "gray", "--allow-color-change", "-o", str(out)]
        )
        == 0
    )
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["state"] == "completed"
    assert any("Color" in message or "color" in message for message in snapshot["warnings"])


@pytest.mark.parametrize(
    "options",
    [
        {"colorspace": "lab"},
        {"colorspace": 1},
        {"allow_color_change": "true"},
        {"allow_color_change": 1},
        {"components": 2},
        {"pages": "1"},
        {"profile_url": "https://example.invalid/profile.icc"},
    ],
)
def test_recolor_invalid_options_rejected_before_worker(tmp_path, source, options):
    with pytest.raises(JobError):
        run(tmp_path, "recolor", source, options)
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize("case", ["page-count", "image-header", "quota", "password", "malformed"])
def test_recolor_failures_do_not_publish_and_allow_recovery(tmp_path, source, case):
    path = tmp_path / "limited.pdf"
    limits = Limits()
    if case == "image-header":
        with pikepdf.open(source) as pdf:
            image = next(iter(pdf.pages[0].get_images().values()))
            image.Width = 16001
            image.Height = 1
            pdf.save(path)
    elif case == "malformed":
        path.write_bytes(b"%PDF-1.7\nbounded invalid fixture")
    else:
        with pymupdf.open() as doc:
            for _ in range(101 if case == "page-count" else 1):
                doc.new_page().insert_text((30, 40), "Source retained")
            if case == "password":
                doc.save(
                    path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner"
                )
            else:
                doc.save(path)
    if case == "quota":
        limits = replace(limits, max_output_bytes=512)
    options = {"allow_color_change": True}
    failed = run(tmp_path, "recolor", path, options, limits=limits)
    assert failed.state == "failed", failed.snapshot()
    assert not failed.results and not list(failed.directory.iterdir())
    recovered = run(tmp_path, "recolor", source, options)
    assert recovered.state == "completed", recovered.snapshot()
    if case == "password":
        recovered = run(tmp_path, "recolor", path, {**options, "password": "secret"})
        assert recovered.state == "completed", recovered.snapshot()


def test_recolor_visible_inline_image_is_counted_and_converted(tmp_path):
    source = tmp_path / "inline.pdf"
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(100, 100))
        page.Contents = pdf.make_stream(
            b"q 50 0 0 25 10 10 cm BI /W 2 /H 1 /CS /RGB /BPC 8 ID \xff\x00\x00\x00\x00\xff EI Q"
        )
        pdf.save(source)
    job = run(tmp_path, "recolor", source, {"allow_color_change": True})
    assert job.state == "completed", job.snapshot()
    report = json.loads(result(job, ".json").read_text())
    assert report["visible_image_instances_checked"] == 1
    assert report["visible_image_pixels_checked"] == 2


def test_recolor_after_grayscale_cannot_restore_lost_colors(tmp_path, source):
    gray = run(tmp_path, "recolor", source, {"allow_color_change": True})
    assert gray.state == "completed"
    rgb = run(
        tmp_path, "recolor", result(gray, ".pdf"), {"colorspace": "rgb", "allow_color_change": True}
    )
    assert rgb.state == "completed"
    with pymupdf.open(result(rgb, ".pdf")) as doc:
        pix = doc[0].get_pixmap()
        for point in [(60, 55), (175, 50), (180, 125)]:
            channels = pix.pixel(*point)
            assert max(channels) - min(channels) <= 1
