"""Real bounded PDF Oxide conversions, including source and package correctness checks."""

import hashlib
import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pikepdf
import pymupdf
import pytest
from PIL import Image

from pdf_multitool.core import Job, JobError, Limits
from pdf_multitool.oxide import availability
from pdf_multitool.oxide_conversion import office_preflight


def pdf_fixture(path, pages=1):
    with pymupdf.open() as document:
        for index in range(pages):
            page = document.new_page(width=240, height=160)
            page.draw_rect((10, 10, 50, 50), color=(1, 0, 0), fill=(1, 0, 0))
            page.insert_text((60, 35), f"PAGE {index + 1:03d}", fontsize=12)
            page.insert_text((20, 80), "Key    Amount    Tax", fontsize=10)
            page.insert_text((20, 100), "012    0.15      -2.00", fontsize=10)
        document.save(path)
    return path


def run(tmp_path, operation, source, options=None, limits=None):
    if not availability()["available"]:
        pytest.skip("Optional pinned PDF Oxide requires supported nonroot Linux")
    inputs = source if isinstance(source, list) else [source]
    original = [path.read_bytes() for path in inputs]
    files = [
        {"id": str(index), "path": str(path), "name": path.name}
        for index, path in enumerate(inputs)
    ]
    job = Job(operation, files, options or {}, tmp_path / "jobs", limits or Limits())
    job.run()
    assert [path.read_bytes() for path in inputs] == original
    return job


def output(job, suffix):
    paths = [Path(item["path"]) for item in job.results if item["path"].endswith(suffix)]
    assert len(paths) == 1, job.snapshot()
    return paths[0]


def test_png_render_preserves_dimensions_color_crop_rotation_and_identity(tmp_path):
    source = pdf_fixture(tmp_path / "source.pdf")
    with pymupdf.open(source) as document:
        document[0].set_cropbox(pymupdf.Rect(0, 0, 200, 120))
        document[0].set_rotation(90)
        document.save(tmp_path / "cropped.pdf")
    source = tmp_path / "cropped.pdf"
    job = run(tmp_path, "oxide_render", source)
    assert job.state == "completed", job.errors
    with Image.open(output(job, ".png")) as image:
        assert image.size == (120, 200)
        assert image.convert("RGB").getpixel((90, 30)) == (255, 0, 0)
    report = json.loads(output(job, ".json").read_text())
    assert report["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report["pages"][0]["page"] == 1
    assert report["fidelity_verified"] is False


@pytest.mark.parametrize("kind", ["png", "jpeg"])
def test_page_fit_and_transparent_png(tmp_path, kind):
    source = pdf_fixture(tmp_path / "source.pdf")
    job = run(
        tmp_path,
        "oxide_render",
        source,
        {"format": kind, "sizing": "fit", "width": 80, "height": 70},
    )
    assert job.state == "completed", job.errors
    with Image.open(output(job, f".{kind}")) as image:
        assert image.width == 80 and image.height <= 70
    if kind == "png":
        transparent = run(tmp_path, "oxide_render", source, {"transparent": True})
        assert transparent.state == "completed", transparent.errors
        with Image.open(output(transparent, ".png")) as image:
            assert image.mode == "RGBA" and image.getpixel((230, 150))[3] == 0


def test_raw_pixels_have_explicit_premultiplied_format(tmp_path):
    source = pdf_fixture(tmp_path / "source.pdf")
    job = run(tmp_path, "oxide_render", source, {"format": "pixmap"})
    assert job.state == "completed", job.errors
    data = output(job, ".rgba").read_bytes()
    assert len(data) == 240 * 160 * 4
    offset = (30 * 240 + 30) * 4
    assert data[offset : offset + 4] == b"\xff\x00\x00\xff"
    info = json.loads(output(job, ".json").read_text())["pages"][0]["outputs"][0]
    assert info["pixel_format"] == "premultiplied RGBA8888"


def test_flatten_is_explicit_preserves_color_and_removes_text_structure(tmp_path):
    source = pdf_fixture(tmp_path / "source.pdf", pages=2)
    with pytest.raises(JobError, match="allow_rasterization"):
        run(tmp_path, "oxide_flatten", source)
    with pytest.raises(JobError, match="allow_page_relayout"):
        run(tmp_path, "oxide_flatten", source, {"allow_rasterization": True})
    job = run(
        tmp_path,
        "oxide_flatten",
        source,
        {"allow_rasterization": True, "allow_page_relayout": True},
    )
    assert job.state == "partial", job.errors
    with pymupdf.open(output(job, ".pdf")) as document:
        assert len(document) == 2
        assert all(not page.get_text().strip() for page in document)
        assert document[0].rect != pymupdf.Rect(0, 0, 240, 160)
        image = document[0].get_images(full=True)[0]
        pixmap = pymupdf.Pixmap(document, image[0])
        offset = (30 * pixmap.width + 30) * pixmap.n
        assert tuple(pixmap.samples[offset : offset + 3]) == (255, 0, 0)
    assert not list(job.directory.rglob("oxide-flatten-*"))
    report = json.loads(output(job, ".json").read_text())
    assert report["redaction_verified"] is False
    assert report["partial"] is True
    assert report["source_page_dimensions"] != report["output_page_dimensions"]
    assert "text layer" in report["lost_structure"]


@pytest.mark.parametrize("mode", ["separation", "separations"])
def test_ink_coverage_uses_app_owned_names_and_preserves_direction(tmp_path, mode):
    source = tmp_path / "cmyk.pdf"
    with pikepdf.new() as document:
        page = document.add_blank_page(page_size=(100, 80))
        page.Contents = document.make_stream(b"1 0 0 0 k 10 10 20 20 re f\n")
        document.save(source)
    job = run(tmp_path, "oxide_render", source, {"format": mode, "ink": "Cyan"})
    assert job.state == "completed", job.errors
    report = json.loads(output(job, ".json").read_text())
    records = report["pages"][0]["outputs"]
    cyan = next(record for record in records if record["ink_name"] == "Cyan")
    path = next(
        Path(item["path"]) for item in job.results if Path(item["path"]).name.endswith(cyan["file"])
    )
    with Image.open(path) as image:
        assert image.size == (100, 80)
        assert image.getpixel((20, 60)) == 255
        assert image.getpixel((80, 60)) == 0


def test_render_pixel_and_output_limits_clean_up(tmp_path):
    source = tmp_path / "large.pdf"
    with pymupdf.open() as document:
        document.new_page(width=2000, height=2000)
        document.save(source)
    job = run(tmp_path, "oxide_render", source, {"dpi": 300})
    assert job.state == "failed" and not job.results
    assert "pixel" in str(job.errors).lower()
    source = pdf_fixture(tmp_path / "source.pdf")
    job = run(tmp_path, "oxide_render", source, limits=replace(Limits(), max_output_bytes=128))
    assert job.state == "failed" and not job.results
    assert not list(job.directory.iterdir())


def test_render_invalid_cross_fields_fail_before_spawning(tmp_path):
    source = pdf_fixture(tmp_path / "source.pdf")
    with pytest.raises(JobError, match="transparent"):
        run(tmp_path, "oxide_render", source, {"format": "jpeg", "transparent": True})
    assert not (tmp_path / "jobs").exists()


def image_fixture(path, kind="PNG", color=(255, 0, 0), mode="RGB"):
    image = Image.new(mode, (80, 60), color)
    image.save(path, format=kind)
    return path


@pytest.mark.parametrize("kind", ["PNG", "JPEG"])
def test_image_pdf_preserves_pixels_and_source(tmp_path, kind):
    source = image_fixture(tmp_path / f"source.{kind.lower()}", kind)
    job = run(tmp_path, "oxide_image_pdf", source)
    assert job.state == "completed", job.errors
    with pymupdf.open(output(job, ".pdf")) as document:
        assert len(document) == 1
        pixmap = document[0].get_pixmap()
        offset = (pixmap.height // 2 * pixmap.width + pixmap.width // 2) * pixmap.n
        red, green, blue = pixmap.samples[offset : offset + 3]
        assert red > 240 and green < 15 and blue < 15
    report = json.loads(output(job, ".json").read_text())
    assert report["sources"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_multiple_images_preserve_input_order_and_remove_snapshots(tmp_path):
    sources = [
        image_fixture(tmp_path / "red.png"),
        image_fixture(tmp_path / "blue.png", color=(0, 0, 255)),
    ]
    job = run(tmp_path, "oxide_images_pdf", sources)
    assert job.state == "completed", job.errors
    with pymupdf.open(output(job, ".pdf")) as document:
        assert len(document) == 2
        for index, channel in ((0, 0), (1, 2)):
            pixmap = document[index].get_pixmap()
            offset = (pixmap.height // 2 * pixmap.width + pixmap.width // 2) * pixmap.n
            assert pixmap.samples[offset + channel] > 240
    assert not list(job.directory.rglob("oxide-images-*"))


def test_transparent_png_and_cmyk_jpeg_construct_valid_derivatives(tmp_path):
    rgba = image_fixture(tmp_path / "alpha.png", color=(255, 0, 0, 128), mode="RGBA")
    cmyk = image_fixture(tmp_path / "cmyk.jpeg", "JPEG", color=(255, 0, 0, 0), mode="CMYK")
    for source in (rgba, cmyk):
        if source == cmyk:
            rejected = run(tmp_path, "oxide_image_pdf", source)
            assert rejected.state == "failed" and not rejected.results
            assert "incorrectly preserves CMYK" in str(rejected.errors)
        job = run(tmp_path, "oxide_image_pdf", source, {"normalize_cmyk": source == cmyk})
        assert job.state == "completed", job.errors
        with pymupdf.open(output(job, ".pdf")) as document:
            pixmap = document[0].get_pixmap()
            offset = (pixmap.height // 2 * pixmap.width + pixmap.width // 2) * pixmap.n
            pixel = tuple(pixmap.samples[offset : offset + 3])
        if source == rgba:
            assert pixel[0] > 240 and 115 < pixel[1] < 140 and 115 < pixel[2] < 140
        else:
            # CMYK rendering is profile dependent; require recognizable cyan.
            assert pixel[0] < 60 and pixel[1] > 140 and pixel[2] > 180
            report = json.loads(output(job, ".json").read_text())
            assert report["sources"][0]["transformation"]["operation"] == "CMYK to RGB using Pillow"


def test_image_count_limit_and_malformed_streams_leave_no_output(tmp_path):
    source = image_fixture(tmp_path / "source.png")
    job = run(tmp_path, "oxide_images_pdf", [source] * 101)
    assert job.state == "failed" and not job.results
    assert "100" in str(job.errors)
    source.write_bytes(b"\x89PNG\r\n\x1a\ntruncated")
    job = run(tmp_path, "oxide_image_pdf", source)
    assert job.state == "failed" and not job.results
    assert not list(job.directory.iterdir())


def test_image_unknown_format_is_explicit_and_clean(tmp_path):
    source = image_fixture(tmp_path / "source.gif", "GIF")
    job = run(tmp_path, "oxide_image_pdf", source)
    assert job.state == "failed" and not job.results
    assert "PNG and JPEG" in str(job.errors)
    assert not list(job.directory.iterdir())


def package_text(path):
    with zipfile.ZipFile(path) as package:
        return "\n".join(
            package.read(name).decode("utf-8")
            for name in package.namelist()
            if name.endswith(".xml")
        )


@pytest.mark.parametrize("kind", ["docx", "xlsx", "pptx"])
def test_office_export_keeps_numeric_lexemes_and_reports_unverified_semantics(tmp_path, kind):
    source = pdf_fixture(tmp_path / "source.pdf", pages=2)
    job = run(tmp_path, "oxide_office_export", source, {"format": kind})
    assert job.state == ("completed" if kind == "docx" else "partial"), job.errors
    text = package_text(output(job, f".{kind}"))
    for token in ("001", "002", "012", "0.15", "-2.00"):
        assert token in text
    report = json.loads(output(job, ".json").read_text())
    assert report["upstream_layout_policy"] == "positioned"
    assert report["text_numbers_tables_verified"] is False
    assert report["known_graphics_gap"] is (kind in {"xlsx", "pptx"})
    if kind in {"xlsx", "pptx"}:
        # Preserve the observed failure as a partial-result regression. The
        # red source vector is absent from the native Office XML output.
        assert "FF0000" not in text.upper()
        assert report["source_vector_pages"]
    imported = run(tmp_path, "oxide_office_import", output(job, f".{kind}"))
    assert imported.state == "completed", imported.errors
    with pymupdf.open(output(imported, ".pdf")) as document:
        extracted = "\n".join(page.get_text() for page in document)
    for token in ("012", "0.15", "-2.00"):
        assert token in extracted


@pytest.mark.parametrize(
    "kind,count,policy",
    [
        ("docx", 30, "positioned"),
        ("docx", 31, "flow"),
        ("pptx", 30, "positioned"),
        ("pptx", 31, "flow"),
        ("xlsx", 200, "positioned"),
        ("xlsx", 201, "flow"),
    ],
)
def test_office_layout_transition_is_real_and_explicit(tmp_path, kind, count, policy):
    source = pdf_fixture(tmp_path / "source.pdf", pages=count)
    job = run(tmp_path, "oxide_office_export", source, {"format": kind})
    assert job.state == ("completed" if kind == "docx" else "partial"), job.errors
    report = json.loads(output(job, ".json").read_text())
    assert report["source"]["pages"] == count
    assert report["upstream_layout_policy"] == policy
    text = package_text(output(job, f".{kind}"))
    assert f"{count:03d}" in text
    if kind == "docx":
        assert ("w:framePr" in text) == (policy == "positioned")
    elif kind == "xlsx":
        with zipfile.ZipFile(output(job, ".xlsx")) as package:
            has_drawings = any(
                name.startswith("xl/drawings/") and name.endswith(".xml")
                for name in package.namelist()
            )
        assert has_drawings == (policy == "positioned")


def minimal_docx(body="<w:p><w:r><w:t>Original 012 0.15 -2.00</w:t></w:r></w:p>", extra=None):
    stream = io.BytesIO()
    contents = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.wordprocessingml.document.main+xml"/></Types>'
        ),
        "_rels/.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
        ),
        "word/document.xml": f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>',
        **(extra or {}),
    }
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, value in contents.items():
            package.writestr(name, value)
    return stream.getvalue()


def test_office_native_import_independent_docx(tmp_path):
    source = tmp_path / "original.docx"
    source.write_bytes(minimal_docx())
    job = run(tmp_path, "oxide_office_import", source)
    assert job.state == "completed", job.errors
    with pymupdf.open(output(job, ".pdf")) as document:
        assert "Original 012 0.15 -2.00" in " ".join(page.get_text() for page in document)


def minimal_other_office(kind):
    root = "xl" if kind == "xlsx" else "ppt"
    main = "workbook" if kind == "xlsx" else "presentation"
    main_type = "spreadsheetml.sheet" if kind == "xlsx" else "presentationml.presentation"
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    office_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    content_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    slide_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    drawing_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    contents = {
        "[Content_Types].xml": (
            f'<Types xmlns="{content_ns}"><Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            f'<Override PartName="/{root}/{main}.xml" ContentType="application/vnd.'
            f'openxmlformats-officedocument.{main_type}.main+xml"/></Types>'
        ),
        "_rels/.rels": (
            f'<Relationships xmlns="{rel_ns}"><Relationship Id="rId1" '
            f'Type="{office_ns}/officeDocument" Target="{root}/{main}.xml"/></Relationships>'
        ),
    }
    if kind == "xlsx":
        contents.update(
            {
                "xl/workbook.xml": (
                    f'<workbook xmlns="{spreadsheet_ns}" xmlns:r="{office_ns}">'
                    '<sheets><sheet name="Invoice" sheetId="1" r:id="rId1"/></sheets></workbook>'
                ),
                "xl/_rels/workbook.xml.rels": (
                    f'<Relationships xmlns="{rel_ns}"><Relationship Id="rId1" '
                    f'Type="{office_ns}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
                ),
                "xl/worksheets/sheet1.xml": (
                    f'<worksheet xmlns="{spreadsheet_ns}"><dimension ref="A1:C2"/><sheetData>'
                    '<row r="1"><c r="A1" t="inlineStr"><is><t>Key</t></is></c>'
                    '<c r="B1" t="inlineStr"><is><t>Amount</t></is></c>'
                    '<c r="C1" t="inlineStr"><is><t>Tax</t></is></c></row>'
                    '<row r="2"><c r="A2" t="inlineStr"><is><t>012</t></is></c>'
                    '<c r="B2"><v>0.15</v></c><c r="C2"><v>-2.00</v></c></row>'
                    "</sheetData></worksheet>"
                ),
            }
        )
    else:
        contents.update(
            {
                "ppt/presentation.xml": (
                    f'<p:presentation xmlns:p="{slide_ns}" xmlns:r="{office_ns}">'
                    '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
                    '<p:sldSz cx="9144000" cy="6858000"/></p:presentation>'
                ),
                "ppt/_rels/presentation.xml.rels": (
                    f'<Relationships xmlns="{rel_ns}"><Relationship Id="rId1" '
                    f'Type="{office_ns}/slide" Target="slides/slide1.xml"/></Relationships>'
                ),
                "ppt/slides/slide1.xml": (
                    f'<p:sld xmlns:p="{slide_ns}" xmlns:a="{drawing_ns}"><p:cSld><p:spTree>'
                    '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Invoice"/><p:cNvSpPr/>'
                    '<p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="914400" y="914400"/>'
                    '<a:ext cx="7315200" cy="914400"/></a:xfrm></p:spPr><p:txBody>'
                    '<a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr sz="1800"/>'
                    "<a:t>Original 012 0.15 -2.00</a:t></a:r></a:p></p:txBody>"
                    "</p:sp></p:spTree></p:cSld></p:sld>"
                ),
            }
        )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, value in contents.items():
            package.writestr(name, value)
    return stream.getvalue()


@pytest.mark.parametrize("kind", ["xlsx", "pptx"])
def test_office_native_import_independent_package(tmp_path, kind):
    source = tmp_path / f"original.{kind}"
    source.write_bytes(minimal_other_office(kind))
    job = run(tmp_path, "oxide_office_import", source)
    assert job.state == "completed", job.errors
    with pymupdf.open(output(job, ".pdf")) as document:
        text = " ".join(page.get_text() for page in document)
    assert "012" in text and "0.15" in text and "-2" in text
    report = json.loads(output(job, ".json").read_text())
    assert report["native_log_status"].startswith("disabled")
    assert report["text_numbers_tables_verified"] is False


def test_office_sparse_huge_grid_and_xml_depth_fail_before_native():
    data = minimal_docx(extra={"word/huge.xml": '<sheet><dimension ref="A1:XFD1048576"/></sheet>'})
    with pytest.raises(JobError, match=r"reference|dimensions"):
        office_preflight(data, "docx")
    data = minimal_docx(extra={"word/deep.xml": "<x>" * 129 + "</x>" * 129})
    with pytest.raises(JobError, match="depth"):
        office_preflight(data, "docx")


@pytest.mark.parametrize(
    "extra,fragment",
    [
        ({"word/vbaProject.bin": b"macro"}, "macros"),
        ({"word/embeddings/oleObject1.bin": b"object"}, "embedded"),
        (
            {
                "word/_rels/document.xml.rels": (
                    '<Relationships><Relationship Target="https://example.invalid/image.png" '
                    'TargetMode="External"/></Relationships>'
                )
            },
            "external",
        ),
        ({"word/comments.xml": '<!DOCTYPE x [<!ENTITY e "EXPANSION">]><x>&e;</x>'}, "DTD"),
        ({"../escape.xml": "<x/>"}, "unsafe"),
        ({"word/oversized.xml": "<x>" + "a" * 500_000 + "</x>"}, "decompression"),
    ],
)
def test_office_active_content_and_bombs_fail_before_native_parse(tmp_path, extra, fragment):
    source = tmp_path / "unsafe.docx"
    source.write_bytes(minimal_docx(extra=extra))
    with pytest.raises(JobError, match=fragment):
        office_preflight(source.read_bytes(), "docx")
    job = run(tmp_path, "oxide_office_import", source)
    assert job.state == "failed" and not job.results
    assert fragment in str(job.errors)
    assert not list(job.directory.iterdir())


def test_office_unknown_legacy_format_and_corrupt_package_fail(tmp_path):
    for suffix, data in (("xls", b"old compound input"), ("docx", b"PK damaged")):
        source = tmp_path / f"unsupported.{suffix}"
        source.write_bytes(data)
        job = run(tmp_path, "oxide_office_import", source)
        assert job.state == "failed" and not job.results
