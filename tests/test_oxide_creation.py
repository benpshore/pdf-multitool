"""Real supervised creation, independent PDF readback and rejected declarations."""

import base64
import hashlib
import io
import json
from dataclasses import asdict, replace
from pathlib import Path

import pikepdf
import pymupdf
import pytest
import reportlab
from PIL import Image

from pdf_multitool import oxide_creation
from pdf_multitool.catalog import validate_options
from pdf_multitool.core import Job, JobError, Limits

pytest.importorskip("pdf_oxide")


def create(tmp_path, document=None, *, options=None, limits=None):
    operation = "oxide_create_text" if document is None else "oxide_create_document"
    options = options if document is None else {"document": json.dumps(document)}
    job = Job(operation, [], options or {}, tmp_path / "jobs", limits or Limits())
    job.run()
    return job


def pdf_result(job):
    assert job.state == "completed", job.errors or job.snapshot()
    return next(Path(r["path"]) for r in job.results if r["name"].endswith(".pdf"))


def manifest(job):
    return json.loads(
        next(Path(r["path"]) for r in job.results if r["name"].endswith(".json")).read_text()
    )


def declaration(elements, **kwargs):
    return {"pages": [{"size": "a4", "elements": elements}], **kwargs}


@pytest.mark.parametrize(
    "kind,content,expected",
    [
        ("text", "Invoice 001\nTotal -12.50\nSecond line 0.00", ["001", "-12.50", "0.00"]),
        ("markdown", "# Invoice 001\n\nTotal **-12.50**\n\n- Item 0.00", ["001", "-12.50", "0.00"]),
        ("html", "<h1>Invoice 001</h1><p>Total <strong>-12.50</strong></p>", ["001", "-12.50"]),
    ],
)
def test_markup_retains_numbers_and_hashes(tmp_path, kind, content, expected):
    job = create(
        tmp_path, options={"format": kind, "content": content, "title": "Synthetic invoice"}
    )
    path = pdf_result(job)
    with pymupdf.open(path) as pdf:
        text = "\n".join(page.get_text() for page in pdf)
        assert all(token in text for token in expected)
        assert pdf.metadata["title"] == "Synthetic invoice"
        pix = pdf[0].get_pixmap(matrix=pymupdf.Matrix(1, 1))
        assert min(pix.samples) < 64
    report = manifest(job)
    assert report["source_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert report["output_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report["truncated"] is False
    assert report["review_required"] is True
    integrity = report["independent_validation"]
    assert integrity["pages_checked"] >= 1
    assert integrity["expected_text_tokens"] > 0
    assert integrity["visual_fidelity_verified"] is False


def test_markdown_template_is_present(tmp_path):
    job = create(
        tmp_path,
        options={
            "format": "markdown",
            "content": "# Body 123",
            "header": "Repeated header",
            "footer": "Footer {page}",
        },
    )
    with pymupdf.open(pdf_result(job)) as pdf:
        text = pdf[0].get_text()
        assert "Repeated header" in text
        assert "Body 123" in text
        assert "Footer" in text


@pytest.fixture
def font_asset():
    # ReportLab's redistributable Bitstream Vera font is public test material.
    data = (Path(reportlab.__file__).parent / "fonts" / "Vera.ttf").read_bytes()
    return {"name": "Vera", "data": base64.b64encode(data).decode()}


@pytest.mark.parametrize("two_fonts", [False, True])
def test_html_css_embedded_fonts_and_separate_lines(tmp_path, font_asset, two_fonts):
    fonts = [font_asset]
    if two_fonts:
        # Same public bytes under two family names exercise the actual cascade binding.
        fonts.append({**font_asset, "name": "Fallback"})
    if two_fonts:
        # The bounded request would reject two full fonts. Subset the fixture to
        # its required glyphs, without weakening the production input limit.
        from fontTools import subset
        from fontTools.ttLib import TTFont

        font = TTFont(io.BytesIO(base64.b64decode(font_asset["data"])))
        subsetter = subset.Subsetter()
        subsetter.populate(text="Item Amount A001 -12.50")
        subsetter.subset(font)
        output = io.BytesIO()
        font.save(output)
        fonts = [
            {"name": name, "data": base64.b64encode(output.getvalue()).decode()}
            for name in ("Vera", "Fallback")
        ]
    job = create(
        tmp_path,
        options={
            "format": "html_css",
            "content": "<p>Item Amount</p><p>A001 -12.50</p>",
            "css": "html,body,p {display:block} p {font-family: Vera; font-size: 14px; "
            "margin:10px; height:24px}",
            "fonts": json.dumps(fonts),
        },
    )
    with pymupdf.open(pdf_result(job)) as pdf:
        text = "\n".join(page.get_text() for page in pdf)
        assert all(value in text for value in ("Item", "Amount", "A001", "-12.50"))
        assert any(font[1] != "n/a" for font in pdf[0].get_fonts())
        words = {word[4]: word for word in pdf[0].get_text("words")}
        assert words["A001"][1] > words["Item"][3]


def test_declarative_text_graphics_geometry_and_tagging(tmp_path):
    elements = [
        {"type": "text", "text": "Original 001 -12.50", "x": 72, "y": 740, "size": 14},
        {"type": "heading", "level": 2, "text": "Heading"},
        {"type": "paragraph", "text": "Preserved paragraph and number 0.15."},
        {"type": "inline", "text": "Inline "},
        {"type": "inline_bold", "text": "bold "},
        {"type": "inline_italic", "text": "italic "},
        {"type": "inline_color", "text": "blue", "color": [0, 0, 1]},
        {"type": "newline"},
        {"type": "space", "points": 10},
        {"type": "horizontal_rule"},
        {"type": "rect", "x": 72, "y": 300, "width": 100, "height": 60},
        {"type": "filled_rect", "x": 200, "y": 300, "color": [1, 0, 0]},
        {"type": "line", "x": 72, "y": 260, "x2": 300, "y2": 260},
        {"type": "stroke_rect", "x": 330, "y": 300, "stroke": 2},
        {"type": "stroke_rect_dashed", "x": 330, "y": 200, "dash": [3, 2]},
        {"type": "stroke_line", "x": 72, "y": 250, "x2": 300, "y2": 250},
        {"type": "stroke_line_dashed", "x": 72, "y": 240, "x2": 300, "y2": 240},
        {
            "type": "text_in_rect",
            "x": 72,
            "y": 190,
            "width": 200,
            "height": 40,
            "text": "Box 123.45",
            "align": "center",
        },
        {"type": "footnote", "reference": "1", "text": "Citation 2026", "x": 72, "y": 150},
        {"type": "new_page"},
        {"type": "columns", "text": "Column prose 001. " * 10, "count": 2},
    ]
    document = declaration(
        elements,
        tagged=True,
        language="en-GB",
        role_map={"Body": "P"},
        metadata={
            "title": "Synthetic layout",
            "author": "Fixture",
            "subject": "Creation",
            "creator": "pytest",
        },
    )
    job = create(tmp_path, document)
    with pymupdf.open(pdf_result(job)) as pdf:
        assert len(pdf) == 2
        text = "\n".join(page.get_text() for page in pdf)
        assert all(value in text for value in ("001", "-12.50", "0.15", "Box", "123.45", "2026"))
        assert len(pdf[0].get_drawings()) >= 7
        assert tuple(round(x) for x in pdf[0].rect[2:]) == (595, 842)
    with pikepdf.open(pdf_result(job)) as pdf:
        assert pdf.Root.Lang == "en-GB"
        assert pdf.Root.MarkInfo.Marked
        assert "/StructTreeRoot" in pdf.Root
        assert str(pdf.Root.StructTreeRoot.RoleMap.Body) == "/P"
    assert manifest(job)["text_metrics"][0]["width_pt"] > 10


@pytest.mark.parametrize(
    "kind,mode",
    [
        ("table", "fixed"),
        ("streaming_table", "fixed"),
        ("streaming_table", "sample"),
    ],
)
def test_visually_broken_native_tables_are_blocked(tmp_path, kind, mode):
    element = {
        "type": kind,
        "columns": [
            {"header": "ID", "width": 100},
            {"header": "Amount", "width": 100, "align": "right"},
            {"header": "Tax", "width": 100},
        ],
        "rows": [["001", "-12.50", ""], ["002", "", "0.15"], ["003", "0.00", "1.23"]],
        "header": True,
        "x": 72,
        "y": 740,
    }
    if kind == "streaming_table":
        element.pop("header")
        element.update(mode=mode, repeat_header=True, batch_size=1)
    with pytest.raises(JobError, match="crosses cell borders"):
        create(tmp_path, declaration([element]))
    assert not (tmp_path / "jobs").exists()


def test_streaming_rowspan_also_blocks_broken_layout(tmp_path):
    element = {
        "type": "streaming_table",
        "columns": [{"header": "ID", "width": 120}, {"header": "Number", "width": 120}],
        "rows": [[{"text": "Spanning", "rowspan": 2}, "001"], ["", "002"]]
        + [[f"ROW{i:03d}", f"{i}.25"] for i in range(100)],
        "repeat_header": True,
    }
    with pytest.raises(JobError, match="crosses cell borders"):
        create(tmp_path, declaration([element]))


@pytest.mark.parametrize(
    "symbology,data",
    [
        ("qr", "Synthetic QR 001"),
        ("code128", "ABC123"),
        ("code39", "ABC123"),
        ("ean13", "5901234123457"),
        ("ean8", "96385074"),
        ("upca", "012345678905"),
        ("itf", "12345678"),
        ("code93", "ABC123"),
        ("codabar", "A12345B"),
    ],
)
def test_actual_barcode_image_content(tmp_path, symbology, data):
    job = create(
        tmp_path,
        declaration(
            [
                {
                    "type": "barcode",
                    "symbology": symbology,
                    "data": data,
                    "width": 250,
                    "height": 250 if symbology == "qr" else 80,
                }
            ]
        ),
    )
    with pymupdf.open(pdf_result(job)) as pdf:
        images = pdf[0].get_images(full=True)
        assert len(images) == 1
        image = pdf.extract_image(images[0][0])
        with Image.open(io.BytesIO(image["image"])) as decoded:
            colors = decoded.convert("L").getextrema()
            assert colors[0] < 50 and colors[1] > 200


def test_images_alt_and_artifact_are_embedded(tmp_path):
    source = io.BytesIO()
    Image.new("RGBA", (20, 20), (255, 0, 0, 128)).save(source, format="PNG")
    payload = source.getvalue()
    document = declaration(
        [
            {"type": "image", "asset": "Icon", "alt": "Red icon", "x": 72, "y": 700},
            {"type": "image", "asset": "Icon", "decorative": True, "x": 200, "y": 700},
        ],
        images=[{"name": "Icon", "data": base64.b64encode(payload).decode()}],
        tagged=True,
    )
    job = create(tmp_path, document)
    with pymupdf.open(pdf_result(job)) as pdf:
        assert len(pdf[0].get_image_info()) == 2
        pix = pdf[0].get_pixmap()
        assert min(pix.samples) < 200
    report = manifest(job)
    assert report["images"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert report["images"][0]["width"] == 20


def test_embedded_font_and_missing_glyph_fail_closed(tmp_path, font_asset):
    doc = declaration(
        [{"type": "text", "text": "Résumé 12.50", "font": "Vera"}], fonts=[font_asset]
    )
    job = create(tmp_path, doc)
    with pymupdf.open(pdf_result(job)) as pdf:
        assert "Résumé 12.50" in pdf[0].get_text()
    doc["pages"][0]["elements"][0]["text"] = "Missing: 漢"
    bad = create(tmp_path, doc)
    assert bad.state == "failed" and not bad.results


def test_all_field_types_actual_values_and_unsigned_placeholder(tmp_path):
    elements = [
        {"type": "text_field", "name": "Amount", "value": "001.25", "y": 700},
        {"type": "checkbox", "name": "Accepted", "checked": True, "y": 640},
        {"type": "combo_box", "name": "Choice", "choices": ["A", "B"], "selected": "B", "y": 580},
        {"type": "push_button", "name": "Button", "caption": "Inert", "y": 420},
        {"type": "signature_field", "name": "Unsigned", "y": 340},
    ]
    job = create(tmp_path, declaration(elements))
    with pikepdf.open(pdf_result(job)) as pdf:
        fields = {str(f.T): f for f in pdf.Root.AcroForm.Fields}
        assert str(fields["Amount"].V) == "001.25"
        assert str(fields["Accepted"].V) == "/Yes"
        assert str(fields["Choice"].V) == "B"
        assert str(fields["Unsigned"].FT) == "/Sig"
        assert "/V" not in fields["Unsigned"]
        assert all("/AA" not in field for field in fields.values())


@pytest.mark.parametrize(
    "element,reason",
    [
        ({"type": "radio_group", "name": "Group", "buttons": [{"value": "A"}]}, "unlinked"),
        (
            {
                "type": "streaming_table",
                "columns": [{"width": 100}],
                "rows": [["001"], ["002"]],
                "mode": "auto_all",
            },
            "dropped every tested cell",
        ),
    ],
)
def test_known_native_data_loss_is_blocked_before_spawn(tmp_path, element, reason):
    with pytest.raises(JobError, match=reason):
        create(tmp_path, declaration([element]))
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize(
    "content",
    [
        "<script>alert(1)</script>",
        '<img src="file:///etc/passwd">',
        '<p onclick="evil()">X</p>',
        '<iframe src="https://example.com"></iframe>',
        '<p style="background: u\\72l(file:///etc/passwd)">X</p>',
        '<a href="javascript:evil()">X</a>',
        '<link rel="stylesheet" href="file:///etc/passwd">',
    ],
)
def test_reject_active_or_external_html_before_spawn(tmp_path, content):
    with pytest.raises(JobError):
        create(tmp_path, options={"format": "html", "content": content})
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize(
    "element",
    [
        {"type": "on_open", "script": "evil"},
        {"type": []},
        {"type": "text", "path": "/etc/passwd"},
        {"type": "image", "asset": "Unknown"},
        {"type": "image", "asset": []},
        {"type": "text", "size": float("inf")},
        {"type": "barcode", "data": "ABC", "width": 200000},
        {"type": "rect", "width": -2},
        {"type": "link_url", "url": "file:///etc/passwd"},
        {"type": "table", "columns": [{"width": 400}, {"width": 400}], "rows": [["a", "b"]]},
        {"type": "table", "columns": [{"width": 100}], "rows": [["a", "b"]]},
    ],
)
def test_reject_invalid_creation_before_spawn(tmp_path, element):
    with pytest.raises(JobError):
        create(tmp_path, declaration([element]))
    assert not (tmp_path / "jobs").exists()


def test_limits_empty_malformed_and_output_cleanup(tmp_path):
    for value in ('{"pages":[],"pages":[]}', '{"pages":[]}', '{"pages":NaN}'):
        with pytest.raises(JobError):
            validate_options("oxide_create_document", {"document": value})
    with pytest.raises(JobError):
        oxide_creation.validate_options(
            "oxide_create_document",
            {"document": json.dumps(declaration([{"type": "text", "text": "x"}] * 1001))},
        )
    job = create(
        tmp_path,
        declaration([{"type": "text", "text": "Limit fixture"}]),
        limits=replace(Limits(), max_output_bytes=1024),
    )
    assert job.state == "failed" and job.results == []
    assert list(job.directory.iterdir()) == []


def test_malformed_asset_fails_without_partial_output(tmp_path):
    job = create(
        tmp_path,
        declaration(
            [{"type": "image", "asset": "Bad", "alt": "bad"}],
            images=[{"name": "Bad", "data": base64.b64encode(b"not an image").decode()}],
        ),
    )
    assert job.state == "failed" and job.results == []
    assert list(job.directory.iterdir()) == []


@pytest.mark.parametrize("kind", ["highlight", "underline", "strikeout", "squiggly", "stamp"])
def test_visually_broken_annotations_fail_before_spawn(tmp_path, kind):
    with pytest.raises(JobError, match=r"visual checks|visible PDFium"):
        create(tmp_path, declaration([{"type": "text", "text": "Target"}, {"type": kind}]))


def test_links_notes_freetext_and_watermarks(tmp_path):
    document = {
        "pages": [
            {
                "size": "a4",
                "elements": [
                    {"type": "text", "text": "External link", "x": 72, "y": 740},
                    {"type": "link_url", "url": "https://example.com/"},
                    {"type": "text", "text": "Next page", "x": 72, "y": 700},
                    {"type": "link_page", "page": 2},
                    {"type": "sticky_note", "text": "Synthetic note"},
                    {"type": "sticky_note_at", "x": 72, "y": 600, "text": "Placed note"},
                    {"type": "freetext", "x": 72, "y": 500, "text": "Note 123", "width": 150},
                    {"type": "watermark_draft"},
                ],
            },
            {
                "size": [900, 1200],
                "elements": [
                    {"type": "text", "text": "Second 002"},
                    {"type": "watermark_confidential"},
                ],
            },
            {
                "size": [700, 900],
                "elements": [
                    {"type": "text", "text": "Third 003"},
                    {"type": "watermark", "text": "TEST"},
                ],
            },
        ]
    }
    job = create(tmp_path, document)
    with pymupdf.open(pdf_result(job)) as pdf:
        links = pdf[0].get_links()
        assert any(link.get("uri") == "https://example.com/" for link in links)
        contents = [a.info["content"] for a in pdf[0].annots()]
        assert "Synthetic note" in contents and "Placed note" in contents and "Note 123" in contents
        assert tuple(pdf[2].rect[2:]) == (700, 900)
    with pikepdf.open(pdf_result(job)) as pdf:
        destinations = [a.Dest for a in pdf.pages[0].Annots if "/Dest" in a]
        assert len(destinations) == 1
        assert destinations[0][0].objgen == pdf.pages[1].objgen
        assert str(destinations[0][1]) == "/Fit"


def test_clipped_watermark_and_invalid_internal_link_fail_without_output(tmp_path):
    for document in (
        {"pages": [{"size": [400, 250], "elements": [{"type": "watermark_draft"}]}]},
        declaration([{"type": "text", "text": "Target"}, {"type": "link_page", "page": 2}]),
    ):
        job = create(tmp_path, document)
        assert job.state == "failed" and job.results == []


def test_left_template_artifacts(tmp_path):
    job = create(
        tmp_path,
        options={
            "format": "markdown",
            "content": "Body 001",
            "header": "Centered",
            "header_left": "Left header",
            "footer_left": "Left footer",
        },
    )
    with pymupdf.open(pdf_result(job)) as pdf:
        text = pdf[0].get_text()
        assert all(value in text for value in ("Centered", "Left header", "Left footer", "001"))


@pytest.mark.parametrize(
    "symbology,data",
    [
        ("qr", "Synthetic QR 001"),
        ("code128", "ABC123"),
        ("code39", "ABC123"),
        ("ean13", "5901234123457"),
        ("ean8", "96385074"),
        ("upca", "012345678905"),
        ("itf", "12345678"),
        ("code93", "ABC123"),
        ("codabar", "A12345B"),
    ],
)
def test_standalone_svg_barcode_actual_vector_content(tmp_path, symbology, data):
    job = Job(
        "oxide_create_barcode",
        [],
        {"symbology": symbology, "data": data},
        tmp_path / "jobs",
        Limits(),
    )
    job.run()
    assert job.state == "completed", job.snapshot()
    output = next(Path(r["path"]) for r in job.results if r["name"].endswith(".svg"))
    with pymupdf.open(output) as svg:
        bitmap = svg[0].get_pixmap(matrix=pymupdf.Matrix(3, 3))
        assert min(bitmap.samples) < 10 and max(bitmap.samples) > 240
        if symbology == "qr":
            import cv2
            import numpy as np

            pixels = np.frombuffer(bitmap.samples, np.uint8).reshape(
                bitmap.height, bitmap.width, bitmap.n
            )
            decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(pixels)
            assert decoded == data
    assert manifest(job)["payload_sha256"] == hashlib.sha256(data.encode()).hexdigest()


@pytest.mark.parametrize("level", ["low", "medium", "quartile", "high"])
def test_qr_error_correction_levels_decode_exact_payload(tmp_path, level):
    import cv2
    import numpy as np

    payload = "Synthetic 001 -12.50 <&>"
    job = Job(
        "oxide_create_barcode",
        [],
        {"data": payload, "error_correction": level},
        tmp_path / "jobs",
        Limits(),
    )
    job.run()
    assert job.state == "completed", job.snapshot()
    output = next(Path(r["path"]) for r in job.results if r["name"].endswith(".svg"))
    with pymupdf.open(output) as pdf:
        pix = pdf[0].get_pixmap()
        pixels = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
        assert cv2.QRCodeDetector().detectAndDecode(pixels)[0] == payload


@pytest.mark.parametrize(
    "preset",
    [
        "horizontal_stripes",
        "vertical_stripes",
        "checkerboard",
        "dots",
        "diagonal_lines",
        "crosshatch",
    ],
)
def test_pattern_fragment_is_actual_content_not_a_pdf(tmp_path, preset):
    job = Job("oxide_create_pattern", [], {"preset": preset}, tmp_path / "jobs", Limits())
    job.run()
    assert job.state == "completed", job.snapshot()
    output = next(Path(r["path"]) for r in job.results if r["name"].endswith(".txt"))
    assert 0 < output.stat().st_size < 100000
    assert not output.read_bytes().startswith(b"%PDF")
    report = manifest(job)
    assert report["complete_pdf"] is False and report["page_pattern_applied"] is False
    assert report["preset"] == preset


def test_html_tables_and_oversized_images_fail_explicitly(tmp_path, font_asset):
    with pytest.raises(JobError, match="table creation failed visual"):
        create(
            tmp_path,
            options={
                "format": "html_css",
                "content": "<table><tr><td>001</td></tr></table>",
                "fonts": json.dumps([font_asset]),
            },
        )
    source = io.BytesIO()
    Image.new("RGB", (2500, 2000), "white").save(source, format="PNG")
    job = create(
        tmp_path,
        declaration(
            [{"type": "image", "asset": "Large", "alt": "large"}],
            images=[{"name": "Large", "data": base64.b64encode(source.getvalue()).decode()}],
        ),
    )
    assert job.state == "failed" and job.results == []


@pytest.mark.parametrize(
    "kind,content,missing",
    [
        ("markdown", "Invoice 001\n\n`Résumé 漢 123.45`", "漢"),
        ("html", "<p>Amount &#x31;&#x32;&#x33;.45 &apos;retained&apos;</p>", "123.45"),
        ("markdown", "# " + "Total 001 -12.50 " * 40, None),
    ],
)
def test_native_markup_loss_is_reproduced_and_not_published(tmp_path, kind, content, missing):
    import pdf_oxide

    options = validate_options("oxide_create_text", {"format": kind, "content": content})
    native = (
        pdf_oxide.Pdf.from_markdown(content)
        if kind == "markdown"
        else pdf_oxide.Pdf.from_html(content)
    )
    raw = bytes(native.to_bytes())
    # Keep actual native bytes and exact synthetic input even though the Job
    # correctly refuses to publish them. These are native defects, not mocks.
    (tmp_path / "native-output.pdf").write_bytes(raw)
    (tmp_path / "source.json").write_text(json.dumps(options, ensure_ascii=False, indent=2))
    with pymupdf.open(stream=raw, filetype="pdf") as pdf:
        assert len(pdf) == int(pdf_oxide.PdfDocument.from_bytes(raw).page_count)
        assert not pdf.is_repaired
        text = "\n".join(page.get_text() for page in pdf)
        pdf[0].get_pixmap().save(tmp_path / "native-render.png")
    (tmp_path / "extracted.txt").write_text(text)
    if missing:
        assert missing not in text
    else:
        assert text.count("-12.50") < content.count("-12.50")
    job = create(tmp_path, options=options)
    assert job.state == "failed" and job.results == []
    assert "missing requested text/numbers" in str(job.errors)
    assert list(job.directory.iterdir()) == []


@pytest.mark.parametrize("failure", ["off-page", "columns", "keywords"])
def test_native_declarative_loss_is_preserved_and_rejected(tmp_path, failure):
    import pdf_oxide

    document = declaration([{"type": "text", "text": "Retained 001", "x": 72, "y": 740}])
    if failure == "off-page":
        document["pages"][0]["elements"].append(
            {"type": "text", "text": "MISSING 123.45", "x": 1950, "y": 650}
        )
    elif failure == "columns":
        document["pages"] = [
            {
                "size": [180, 180],
                "elements": [
                    {
                        "type": "columns",
                        "count": 2,
                        "text": " ".join(f"ROW{i:03d} {i}.25" for i in range(80)),
                    }
                ],
            }
        ]
    else:
        document["metadata"] = {"keywords": "Requested keyword 001"}
    raw, _, _ = oxide_creation._build(document, {}, {}, pdf_oxide, lambda stage, call: call())
    (tmp_path / "native-output.pdf").write_bytes(raw)
    (tmp_path / "source.json").write_text(json.dumps(document, indent=2))
    with pymupdf.open(stream=raw, filetype="pdf") as pdf:
        assert not pdf.is_repaired and len(pdf) == 1
        text = pdf[0].get_text()
        assert (
            pdf.metadata["keywords"] == ""
            if failure == "keywords"
            else "123.45" not in text
            if failure == "off-page"
            else "79.25" not in text
        )
        pdf[0].get_pixmap().save(tmp_path / "native-render.png")
    (tmp_path / "extracted.txt").write_text(text)
    job = create(tmp_path, document)
    assert job.state == "failed" and job.results == []
    assert ("metadata" if failure == "keywords" else "missing requested text/numbers") in str(
        job.errors
    )
    assert list(job.directory.iterdir()) == []


@pytest.mark.parametrize(
    "damage", ["repaired-xref", "bad-content-stream", "missing-repetition", "wrong-number"]
)
def test_independent_integrity_checks_block_injected_output_before_write(
    tmp_path, monkeypatch, damage
):
    import pdf_oxide

    from pdf_multitool import oxide_runtime

    source = "Invoice 001 -12.50\nInvoice 001 -12.50"
    raw = bytes(pdf_oxide.Pdf.from_text(source).to_bytes())
    if damage == "repaired-xref":
        raw = raw[: raw.rfind(b"startxref")] + b"startxref\n1\n%%EOF\n"
        reason = "structural repair"
    elif damage == "bad-content-stream":
        with pikepdf.open(io.BytesIO(raw)) as pdf:
            content = pdf.pages[0].Contents.read_bytes()
            pdf.pages[0].Contents = pdf.make_stream(content + b"\ninvalid_operator\n")
            out = io.BytesIO()
            pdf.save(out)
            raw = out.getvalue()
        reason = "decoding reported errors"
    else:
        replacement = (
            source.splitlines()[0]
            if damage == "missing-repetition"
            else source.replace("-12.50", "-12.51")
        )
        raw = bytes(pdf_oxide.Pdf.from_text(replacement).to_bytes())
        reason = "missing requested text/numbers"
    (tmp_path / "controlled-corruption.pdf").write_bytes(raw)
    options = validate_options("oxide_create_text", {"content": source})
    # This direct validator assertion exercises actual independent decoding.
    with pytest.raises(JobError, match=reason):
        oxide_creation._verify_created_pdf(raw, 1, options)
    # Only the native generation result and worker guard are substituted here;
    # independent parsing/checking and the publish boundary remain real.
    monkeypatch.setattr(oxide_runtime, "require_runtime", lambda request: pdf_oxide)
    monkeypatch.setattr(oxide_creation, "_create_text", lambda *args: raw)
    published = []
    monkeypatch.setattr(oxide_runtime, "write_bytes", lambda *args: published.append(args[2]))
    monkeypatch.setattr(oxide_runtime, "write_json", lambda *args: published.append(args[2]))
    request = {
        "operation": "oxide_create_text",
        "limits": asdict(Limits()),
        "workdir": str(tmp_path),
    }
    with pytest.raises(JobError):
        oxide_creation.process(request, options, lambda path: published.append(path))
    assert published == []


def test_independent_count_checks_do_not_certify_visual_layout(tmp_path):
    document = declaration(
        [
            {"type": "text", "text": "Invoice 001 -12.50", "x": 72, "y": 740},
            {"type": "text", "text": "Invoice 001 -12.50", "x": 72, "y": 700},
            {"type": "new_page"},
            {"type": "paragraph", "text": "Wrapped prose 001 -12.50. " * 35},
        ]
    )
    job = create(tmp_path, document)
    pdf_result(job)
    report = manifest(job)
    integrity = report["independent_validation"]
    assert integrity["pages_checked"] >= 2
    assert integrity["visual_fidelity_verified"] is False
    assert any("page/cell association" in item for item in integrity["limitations"])
    limited = create(tmp_path / "limited", document, limits=replace(Limits(), max_pages=1))
    assert limited.state == "failed" and limited.results == []


def test_independent_checks_accept_empty_and_unselected_form_defaults(tmp_path):
    job = create(
        tmp_path,
        declaration(
            [
                {"type": "text_field", "name": "Empty", "value": "", "y": 700},
                {"type": "checkbox", "name": "Unchecked", "checked": False, "y": 640},
                {"type": "combo_box", "name": "NoSelection", "choices": ["A", "B"], "y": 580},
            ]
        ),
    )
    pdf_result(job)
    assert manifest(job)["independent_validation"]["form_fields"] == 3


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("image", "image/barcode placement"),
        ("field", "every requested form field"),
        ("field-value", "form value or choice"),
        ("note", "annotation content"),
    ],
)
def test_independent_checks_detect_missing_objects_and_wrong_field_values(tmp_path, damage, reason):
    import pdf_oxide

    if damage == "image":
        source = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(source, format="PNG")
        document = declaration(
            [{"type": "image", "asset": "Icon", "alt": "Synthetic red icon"}],
            images=[{"name": "Icon", "data": base64.b64encode(source.getvalue()).decode()}],
        )
    elif damage == "note":
        document = declaration([{"type": "sticky_note_at", "text": "Note 001 -12.50"}])
    else:
        document = declaration([{"type": "text_field", "name": "Value", "value": "001.25"}])
    document, fonts, images = oxide_creation._document(json.dumps(document))
    raw, _, _ = oxide_creation._build(
        document, fonts, images, pdf_oxide, lambda stage, call: call()
    )
    oxide_creation._verify_created_pdf(raw, 1, {}, document)
    (tmp_path / "native-original.pdf").write_bytes(raw)
    (tmp_path / "source.json").write_text(json.dumps(document, indent=2))
    with pikepdf.open(io.BytesIO(raw)) as pdf:
        if damage == "image":
            pdf.pages[0].Contents = pdf.make_stream(b"")
        elif damage == "field-value":
            pdf.Root.AcroForm.Fields[0].V = "0099"
        else:
            pdf.pages[0].Annots = pikepdf.Array()
            if damage == "field":
                pdf.Root.AcroForm.Fields = pikepdf.Array()
        output = io.BytesIO()
        pdf.save(output)
    corrupted = output.getvalue()
    (tmp_path / "controlled-corruption.pdf").write_bytes(corrupted)
    with pytest.raises(JobError, match=reason):
        oxide_creation._verify_created_pdf(corrupted, 1, {}, document)
