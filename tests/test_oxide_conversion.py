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
    assert report["output_structure_validation"]["independent_parser"] == "PyMuPDF"
    assert report["partial"] is True
    assert report["source_page_dimensions"] != report["output_page_dimensions"]
    assert "text layer" in report["lost_structure"]


def flatten_geometry_fixture(path, rotation=0, cropped=False, sizes=None, user_unit=None):
    """Distinct corners and numeric/table content authored directly in PDF user space."""
    with pikepdf.new() as document:
        font = document.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
            )
        )
        for width, height in sizes or [(260, 180)]:
            page = document.add_blank_page(page_size=(width, height))
            page.Resources.Font = pikepdf.Dictionary(F1=font)
            x0, y0 = (40, 60) if cropped else (0, 0)
            page.MediaBox = pikepdf.Array([x0, y0, x0 + width, y0 + height])
            if cropped:
                x0, y0, width, height = x0 + 20, y0 + 15, width - 40, height - 30
                page.CropBox = pikepdf.Array([x0, y0, x0 + width, y0 + height])
            page.Rotate = rotation
            if user_unit is not None:
                page.UserUnit = user_unit
            content = (
                f"1 0 0 rg {x0} {y0 + height - 25} 25 25 re f\n"
                f"0 1 0 rg {x0 + width - 25} {y0 + height - 25} 25 25 re f\n"
                f"0 0 1 rg {x0} {y0} 25 25 re f\n"
                f"1 0 1 rg {x0 + width - 25} {y0} 25 25 re f\n"
                f"0 g BT /F1 10 Tf {x0 + 35} {y0 + height - 35} Td "
                "(012   0.15   -2.00) Tj ET\n"
                f"0 G 1 w {x0 + 35} {y0 + 35} 140 45 re S\n"
                f"{x0 + 35} {y0 + 57} m {x0 + 175} {y0 + 57} l S\n"
                f"{x0 + 105} {y0 + 35} m {x0 + 105} {y0 + 80} l S\n"
                f"BT /F1 10 Tf {x0 + 40} {y0 + 63} Td (A1) Tj ET\n"
                f"BT /F1 10 Tf {x0 + 110} {y0 + 63} Td (B1) Tj ET\n"
                f"BT /F1 10 Tf {x0 + 40} {y0 + 41} Td (A2) Tj ET\n"
                f"BT /F1 10 Tf {x0 + 110} {y0 + 41} Td (B2) Tj ET\n"
            )
            page.Contents = document.make_stream(content.encode("ascii"))
        document.save(path)
    return path


def pdfium_page_image(path, index=0, dpi=144):
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(path) as document:
        page = document[index]
        bitmap = page.render(scale=dpi / 72)
        result = bitmap.to_pil().convert("RGB").copy()
        bitmap.close()
        page.close()
    return result


@pytest.mark.parametrize("rotation", [0, 90, 180, 270, -90])
@pytest.mark.parametrize("cropped", [False, True])
def test_explicit_native_flatten_preserves_visible_geometry_and_pdfium_pixels(
    tmp_path, rotation, cropped
):
    from PIL import ImageChops, ImageStat

    source = flatten_geometry_fixture(tmp_path / "geometry.pdf", rotation, cropped)
    job = run(
        tmp_path,
        "oxide_flatten",
        source,
        {
            "layout": "preserve_visible_page",
            "allow_rasterization": True,
            "dpi": 144,
        },
    )
    assert job.state == "completed", job.errors
    derivative = output(job, ".pdf")
    with pymupdf.open(source) as before, pymupdf.open(derivative) as after:
        assert before[0].rect == after[0].rect
        assert after[0].rotation == 0
        assert after[0].mediabox.x0 == after[0].mediabox.y0 == 0
        assert "012" in before[0].get_text() and after[0].get_text() == ""
    original, actual = pdfium_page_image(source), pdfium_page_image(derivative)
    assert original.size == actual.size
    for point in (
        (12, 12),
        (original.width - 12, 12),
        (12, original.height - 12),
        (original.width - 12, original.height - 12),
    ):
        assert original.getpixel(point) == actual.getpixel(point)
    # Independent PDFium render differences are confined to font/raster edges;
    # no full-page padding, crop loss, rotation or marker color changes survive.
    difference = ImageStat.Stat(ImageChops.difference(original, actual))
    assert max(difference.mean) < 3.0
    report = json.loads(output(job, ".json").read_text())
    assert report["operation"] == "render_page+DocumentBuilder"
    assert report["container_writer"] == report["renderer"] == "PDF Oxide"
    assert report["engine_fallback"] is False
    assert report["physical_dimensions_checked"] is True
    assert report["output_structure_validation"]["content_fidelity_checked"] is False
    assert report["fidelity_verified"] is False
    assert report["partial"] is False
    assert report["source_page_dimensions"] == report["output_page_dimensions"]
    assert report["source_geometry"][0]["output_rotation_degrees"] == 0
    assert (
        report["page_rasters"][0]["source_sha256"]
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert not list(job.directory.rglob("oxide-flatten-*"))


@pytest.mark.parametrize("dpi", [72, 144])
def test_preserve_visible_page_handles_mixed_fractional_page_sizes(tmp_path, dpi):
    source = flatten_geometry_fixture(
        tmp_path / "mixed.pdf", sizes=[(240, 160), (320.5, 220.25), (210, 300)]
    )
    job = run(
        tmp_path,
        "oxide_flatten",
        source,
        {
            "layout": "preserve_visible_page",
            "allow_rasterization": True,
            "dpi": dpi,
        },
    )
    assert job.state == "completed", job.errors
    with pymupdf.open(source) as before, pymupdf.open(output(job, ".pdf")) as after:
        assert len(before) == len(after) == 3
        for index in range(3):
            assert tuple(before[index].rect) == pytest.approx(tuple(after[index].rect), abs=0.01)
            assert (
                pdfium_page_image(source, index, dpi).size
                == pdfium_page_image(output(job, ".pdf"), index, dpi).size
            )


def test_preserve_visible_page_embeds_every_native_raster_pixel_unchanged(tmp_path):
    source = flatten_geometry_fixture(tmp_path / "geometry.pdf", rotation=270, cropped=True)
    reference = run(tmp_path, "oxide_render", source, {"dpi": 144})
    flattened = run(
        tmp_path,
        "oxide_flatten",
        source,
        {"layout": "preserve_visible_page", "allow_rasterization": True, "dpi": 144},
    )
    assert reference.state == flattened.state == "completed"
    png = output(reference, ".png").read_bytes()
    with Image.open(io.BytesIO(png)) as image:
        expected = image.convert("RGB")
    with pymupdf.open(output(flattened, ".pdf")) as document:
        images = document[0].get_images(full=True)
        assert len(images) == 1
        actual = pymupdf.Pixmap(document, images[0][0])
        assert (actual.width, actual.height) == expected.size
        assert actual.samples == expected.tobytes()
    report = json.loads(output(flattened, ".json").read_text())
    assert report["page_rasters"][0]["png_sha256"] == hashlib.sha256(png).hexdigest()
    assert report["fidelity_verified"] is False


def test_preserve_visible_page_fails_on_unsupported_units_and_output_quota(tmp_path):
    source = flatten_geometry_fixture(tmp_path / "scaled.pdf", user_unit=2)
    job = run(
        tmp_path,
        "oxide_flatten",
        source,
        {"layout": "preserve_visible_page", "allow_rasterization": True},
    )
    assert job.state == "failed" and not job.results
    assert "geometry" in str(job.errors).lower()
    assert not list(job.directory.iterdir())
    source = flatten_geometry_fixture(tmp_path / "regular.pdf")
    job = run(
        tmp_path,
        "oxide_flatten",
        source,
        {"layout": "preserve_visible_page", "allow_rasterization": True},
        limits=replace(Limits(), max_output_bytes=128),
    )
    assert job.state == "failed" and not job.results
    assert not list(job.directory.iterdir())


def operator_cap_fixture(path):
    """Visible trailing text beyond the native one-million-operator cap."""
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=240)
        page.insert_text((40, 40), "Before cap 012 0.15")
        reference = page.get_contents()[0]
        document.update_stream(
            reference,
            document.xref_stream(reference)
            + b"BT /helv 11 Tf 0 TL 1 0 0 1 40 160 Tm\n"
            + b"T*\n" * 1_000_010
            + b"(After cap 036 0.00) Tj ET\n",
        )
        document.save(path, deflate=True)
    return path


@pytest.mark.parametrize(
    ("operation", "options"),
    [
        ("oxide_render", {}),
        ("oxide_office_export", {"format": "docx"}),
        ("oxide_office_export", {"format": "xlsx"}),
        ("oxide_office_export", {"format": "pptx"}),
        ("oxide_flatten", {"allow_rasterization": True, "allow_page_relayout": True}),
        ("oxide_flatten", {"layout": "preserve_visible_page", "allow_rasterization": True}),
    ],
)
def test_native_operator_cap_cannot_silently_complete_conversion(tmp_path, operation, options):
    source = operator_cap_fixture(tmp_path / "operators.pdf")
    with pymupdf.open(source) as document:
        text = document[0].get_text()
        assert "Before cap 012 0.15" in text and "After cap 036 0.00" in text
    job = run(tmp_path, operation, source, options)
    assert job.state == "partial", job.errors
    report = json.loads(output(job, ".json").read_text())
    assert report["partial"] is True and report["truncated"] is True
    assert report["fidelity_verified"] is False
    assert any(
        record["category"] == "operator_cap_exceeded" and record["page"] is None
        for record in report["structured_warnings"]
    )
    assert any("truncated" in warning for warning in job.warnings)
    if operation == "oxide_render":
        assert report["pages"][0]["truncated"] is True
    elif operation == "oxide_office_export":
        assert report["page_diagnostics"][0]["truncated"] is True
        with zipfile.ZipFile(output(job, "." + options["format"])) as package:
            xml = b" ".join(
                package.read(name) for name in package.namelist() if name.endswith(".xml")
            )
        assert b"Before cap 012 0.15" in xml and b"After cap 036 0.00" not in xml
    elif options.get("layout") == "preserve_visible_page":
        assert report["page_rasters"][0]["truncated"] is True


def test_conversion_diagnostics_respect_page_scope_and_raster_only_scans():
    from pdf_multitool.oxide_conversion import _diagnostic_status

    scoped = [{"category": "operator_cap_exceeded", "page": 0}]
    assert _diagnostic_status(scoped, {0})["truncated"] is True
    assert _diagnostic_status(scoped, {1})["partial"] is False
    global_warning = [{"category": "operator_cap_exceeded", "page": None}]
    assert _diagnostic_status(global_warning, {1})["truncated"] is True
    scan = [{"category": "no_text_layer", "page": 0}]
    assert _diagnostic_status(scan, {0})["partial"] is False
    text_conversion = _diagnostic_status(scan, {0}, needs_text=True)
    assert text_conversion["partial"] is True and text_conversion["ocr_required"] is True
    assert _diagnostic_status([{"category": "glyph_dropped", "page": 0}], {0})["partial"] is True


@pytest.mark.parametrize("problem", ["truncated_pdf", "page_count_disagreement"])
def test_generated_pdf_reopen_rejects_native_success_claim_for_invalid_output(tmp_path, problem):
    from types import SimpleNamespace

    from pdf_multitool.oxide_conversion import _pdf_summary

    # Fault-inject only the native success claim. The independent parser sees
    # real malformed bytes or a real two-page PDF with a false native count.
    data = (
        b"%PDF-1.7\ntruncated native converter output\n"
        if problem == "truncated_pdf"
        else pdf_fixture(tmp_path / "two-pages.pdf", pages=2).read_bytes()
    )
    module = SimpleNamespace(
        PdfDocument=SimpleNamespace(from_bytes=lambda data: SimpleNamespace(page_count=1))
    )
    with pytest.raises(JobError, match=r"independent converted|page count differs"):
        _pdf_summary(module, data)


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
    assert report["output_structure_validation"]["independent_parser"] == "PyMuPDF"


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
    report = json.loads(output(job, ".json").read_text())
    assert report["output_structure_validation"]["independent_parser"] == "PyMuPDF"
    assert report["output_structure_validation"]["content_fidelity_checked"] is False


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
