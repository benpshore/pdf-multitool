"""PY-006 through the actual supervised product; independent geometric fixtures."""

import base64
import hashlib
import io
import json
import struct
import xml.etree.ElementTree as ET
import zlib
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from pdf_multitool.cli import main
from pdf_multitool.core import JobError, Limits
from pdf_multitool.svg import MAX_SVG_BYTES, SVG_NS, validate_svg
from tests.test_extended import result, run


def svg_fixture(path):
    with pymupdf.open() as doc:
        for rotation in (0, 90):
            page = doc.new_page(width=240, height=180)
            page.draw_rect((20, 30, 100, 80), color=None, fill=(1, 0, 0))
            page.draw_rect((140, 30, 210, 70), color=None, fill=(0, 0, 1))
            page.insert_text((20, 125), "SVG 123", fontsize=14)
            page.insert_font(fontname="unicode", fontbuffer=pymupdf.Font("cjk").buffer)
            page.insert_text((20, 100), "\u03b1 é Ж 中", fontname="unicode", fontsize=12)
            pixels = io.BytesIO()
            Image.new("RGB", (12, 10), (0, 180, 0)).save(pixels, format="PNG")
            page.insert_image((150, 100, 210, 150), stream=pixels.getvalue())
            if rotation:
                page.set_cropbox(pymupdf.Rect(10, 20, 230, 160))
                page.set_rotation(rotation)
        doc.new_page(width=72, height=72)  # Independently declared blank truth.
        doc.subset_fonts()
        doc.save(path, deflate=True)
    return path


@pytest.fixture
def source(tmp_path):
    return svg_fixture(tmp_path / "vector-source.pdf")


@pytest.mark.parametrize("mode", ["paths", "text"])
@pytest.mark.parametrize("scale", [0.25, 1, 4])
def test_svg_geometry_text_modes_and_manifest(tmp_path, source, mode, scale):
    job = run(tmp_path, "svg", source, {"text_mode": mode, "scale": scale})
    assert job.state == "completed", job.snapshot()
    assert len(job.results) == 4
    manifest = json.loads(result(job, ".json").read_text())
    assert manifest["feature_id"] == "PY-006"
    assert manifest["visual_review"] == "not_reviewed"
    assert [p["source_page"] for p in manifest["pages"]] == [1, 2, 3]
    assert [p["rotation"] for p in manifest["pages"]] == [0, 90, 0]
    for page, (width, height) in zip(
        manifest["pages"], [(240, 180), (140, 220), (72, 72)], strict=True
    ):
        item = next(r for r in job.results if r["name"] == page["file"])
        data = Path(item["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == page["sha256"]
        xml = ET.fromstring(data)  # noqa: S314 -- bounded generated test fixture
        assert [float(v) for v in xml.attrib["viewBox"].split()] == [
            0,
            0,
            width * scale,
            height * scale,
        ]
        assert page["width_points"] == width * scale
        assert page["height_points"] == height * scale
        if page["source_page"] != 3:
            assert bool(xml.findall(f".//{{{SVG_NS}}}text")) == (mode == "text")
            assert xml.findall(f".//{{{SVG_NS}}}image")
            if mode == "text":
                assert "SVG 123" in "".join(xml.itertext())
                assert "\u03b1 é Ж 中" in "".join(xml.itertext())
    assert any("fonts without embedding" in message for message in job.warnings) == (mode == "text")


def test_svg_page_selection_cli_and_blank_truth(tmp_path, source, capsys):
    before = source.read_bytes()
    output = tmp_path / "cli"
    assert main(["svg", str(source), "--pages", "3", "--scale", "2", "-o", str(output)]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["state"] == "completed"
    svg = next(output.rglob("*.svg"))
    xml = ET.fromstring(svg.read_bytes())  # noqa: S314 -- bounded generated test fixture
    assert xml.attrib["viewBox"] == "0 0 144 144"
    assert not xml.findall(f".//{{{SVG_NS}}}path")
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    "options",
    [
        {"scale": True},
        {"scale": 0},
        {"scale": float("inf")},
        {"text_mode": "embed-fonts"},
        {"url": "https://example.invalid"},
    ],
)
def test_svg_invalid_options_rejected_before_worker(tmp_path, source, options):
    with pytest.raises(JobError):
        run(tmp_path, "svg", source, options)
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize("case", ["page-count", "dimensions", "quota", "password"])
def test_svg_bounds_fail_without_publication_and_allow_recovery(tmp_path, source, case):
    path = tmp_path / "limited.pdf"
    limits = Limits()
    with pymupdf.open() as doc:
        if case == "page-count":
            for _ in range(101):
                doc.new_page()
        elif case == "dimensions":
            doc.new_page(width=15000, height=30)
        else:
            doc.new_page().insert_text((30, 40), "bounded output")
        if case == "password":
            doc.save(
                path,
                encryption=pymupdf.PDF_ENCRYPT_AES_256,
                owner_pw="owner-secret",
                user_pw="test-secret",
            )
        else:
            doc.save(path)
    if case == "quota":
        limits = replace(limits, max_output_bytes=512)
    failed = run(tmp_path, "svg", path, limits=limits)
    assert failed.state == "failed", failed.snapshot()
    assert not failed.results
    assert not list(failed.directory.iterdir())
    good = run(tmp_path, "svg", source, {"pages": "1"})
    assert good.state == "completed", good.snapshot()
    if case == "password":
        unlocked = run(tmp_path, "svg", path, {"password": "test-secret"})
        assert unlocked.state == "completed", unlocked.snapshot()


@pytest.mark.parametrize(
    "inside",
    [
        "<script>alert(1)</script>",
        "<foreignObject><div>unsafe</div></foreignObject>",
        '<image href="http://127.0.0.1/canary"/>',
        '<image href="file:///forbidden-canary"/>',
        '<path onload="alert(1)"/>',
        '<path style="fill:url(http://127.0.0.1/canary)"/>',
        '<path style="@import &#34;http://127.0.0.1/canary&#34;"/>',
    ],
)
def test_svg_output_validator_rejects_active_or_external_content(inside):
    svg = (
        f'<svg xmlns="{SVG_NS}" width="100pt" height="100pt" viewBox="0 0 100 100">{inside}</svg>'
    ).encode()
    with pytest.raises(JobError):
        validate_svg(svg, 100, 100)


def test_svg_xml_entity_and_dimension_failures():
    with pytest.raises(JobError, match="document type or entity"):
        validate_svg(b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///canary">]><svg/>', 100, 100)
    with pytest.raises(JobError, match="dimensions"):
        validate_svg(
            f'<svg xmlns="{SVG_NS}" width="nan" height="1" viewBox="0 0 1 1"/>'.encode(), 1, 1
        )


def test_svg_large_paths_and_element_count_are_bounded():
    with pytest.raises(JobError, match="16 MiB"):
        validate_svg(b"x" * (MAX_SVG_BYTES + 1), 1, 1)
    data = (
        f'<svg xmlns="{SVG_NS}" width="1pt" height="1pt" viewBox="0 0 1 1">'
        + "<g/>" * 100_000
        + "</svg>"
    ).encode()
    with pytest.raises(JobError, match="100,000-element"):
        validate_svg(data, 1, 1)


def test_svg_embedded_image_budget_checks_headers_without_decoding_pixels():
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    # A bounded header canary, not a giant allocated raster or decompression bomb.
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16001, 2, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", b"bounded canary") + chunk(b"IEND", b"")
    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    data = (
        f'<svg xmlns="{SVG_NS}" width="1pt" height="1pt" viewBox="0 0 1 1">'
        f'<image href="{uri}"/></svg>'
    ).encode()
    with pytest.raises(JobError, match="pixel or format limit"):
        validate_svg(data, 1, 1)
