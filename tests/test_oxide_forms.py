"""Synthetic end-to-end form/annotation evidence through the supervised worker."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from xml.etree import ElementTree

import pymupdf
import pytest

from pdf_multitool.core import Job, JobError, Limits
from pdf_multitool.oxide_forms import validate_options

try:
    _INSTALLED = importlib.metadata.version("pdf-oxide") == "0.3.78"
except importlib.metadata.PackageNotFoundError:
    _INSTALLED = False

native = pytest.mark.skipif(
    not _INSTALLED or sys.platform != "linux" or os.getuid() == 0,
    reason="Optional PDF Oxide 0.3.78 requires the guarded nonroot Linux worker",
)


def form_fixture(path):
    """No external assets, real documents, scripts, credentials or signatures."""
    with pymupdf.open() as doc:
        for number in range(2):
            page = doc.new_page(width=400, height=500)
            page.insert_text((40, 30), f"Synthetic form page {number + 1}; total 20.15")
            specs = (
                [
                    ("account", pymupdf.PDF_WIDGET_TYPE_TEXT, "000123.40"),
                    ("consent", pymupdf.PDF_WIDGET_TYPE_CHECKBOX, False),
                    ("choice", pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "Alpha"),
                    ("locked", pymupdf.PDF_WIDGET_TYPE_TEXT, "Retain me"),
                    ("unsigned", pymupdf.PDF_WIDGET_TYPE_SIGNATURE, None),
                ]
                if number == 0
                else [
                    ("notes", pymupdf.PDF_WIDGET_TYPE_TEXT, "Line one\nLine two"),
                    ("short", pymupdf.PDF_WIDGET_TYPE_TEXT, "001"),
                ]
            )
            for i, (name, kind, value) in enumerate(specs):
                widget = pymupdf.Widget()
                widget.field_name, widget.field_type = name, kind
                widget.rect = pymupdf.Rect(40, 55 + i * 65, 300, 105 + i * 65)
                widget.text_font, widget.text_fontsize = "Helv", 14
                widget.text_color = (0, 0, 0)
                if value is not None:
                    widget.field_value = value
                if name == "choice":
                    widget.choice_values = ["Alpha", "Beta"]
                if name == "locked":
                    widget.field_flags = 1
                if name == "notes":
                    widget.field_flags = 1 << 12
                if name == "short":
                    widget.text_maxlen = 3
                page.add_widget(widget)
            page.add_text_annot((330, 450), f"Keep note on page {number + 1}")
        doc.save(path)
    return path


def annotation_fixture(path):
    with pymupdf.open() as doc:
        for number in range(2):
            page = doc.new_page(width=400, height=500)
            page.insert_text(
                (40, 60), f"Synthetic page {number + 1}: Invoice 000123.40; total 20.15"
            )
            page.add_text_annot((40, 90), f"Original note {number + 1}")
            page.insert_link(
                {
                    "kind": pymupdf.LINK_URI,
                    "from": pymupdf.Rect(40, 130, 180, 155),
                    "uri": f"https://example.invalid/source-{number + 1}",
                }
            )
            page.add_highlight_annot(pymupdf.Rect(40, 45, 110, 65))
        doc.save(path)
    return path


def _run(tmp_path, operation, source, options=None, limits=None):
    before = source.read_bytes()
    job = Job(
        operation,
        [{"id": "fixture", "name": source.name, "path": str(source)}],
        options or {},
        tmp_path / "jobs",
        limits or Limits(),
    )
    job.run()
    assert source.read_bytes() == before
    return job


def _output(job, suffix):
    matches = [Path(item["path"]) for item in job.results if item["path"].endswith(suffix)]
    assert len(matches) == 1, job.snapshot()
    return matches[0]


def _report(job):
    return json.loads(_output(job, ".json").read_text())


def _assert_failed(job):
    assert job.state == "failed", job.snapshot()
    assert job.results == []
    assert not list(job.directory.iterdir())


def _highlight_action(page=1):
    return {
        "type": "highlight",
        "page": page,
        "x": 180,
        "y": 200,
        "width": 80,
        "height": 30,
        "color": [1, 0.8, 0],
    }


def _renderer_snapshot(path, page_number, target=None):
    """Two independent viewers, with PDFium's coordinate transform as a geometry oracle."""
    import pypdfium2 as pdfium
    from PIL import Image

    with pymupdf.open(path) as doc:
        page = doc[page_number]
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), annots=True, alpha=False)
        mupdf = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        mupdf_text = page.get_text()
    with pdfium.PdfDocument(path) as pdf:
        pdf.init_forms(pdfium.raw.FPDF_FORMFILLINFO(version=2, xfa_disabled=True))
        page = pdf[page_number]
        bitmap = page.render(scale=2, draw_annots=True, may_draw_forms=True)
        pdfium_image = bitmap.to_pil().convert("RGB")
        textpage = page.get_textpage()
        pdfium_text = textpage.get_text_range()
        textpage.close()
        bounds = None
        if target:
            x, y, width, height = (target[key] for key in ("x", "y", "width", "height"))
            transform = bitmap.get_posconv(page)
            corners = [
                transform.to_bitmap(px, py) for px in (x, x + width) for py in (y, y + height)
            ]
            bounds = (
                min(p[0] for p in corners),
                min(p[1] for p in corners),
                max(p[0] for p in corners),
                max(p[1] for p in corners),
            )
        bitmap.close()
        page.close()
    assert mupdf.size == pdfium_image.size
    return {"mupdf": mupdf, "pdfium": pdfium_image}, (mupdf_text, pdfium_text), bounds


@native
def test_inspection_preserves_exact_values_and_widget_page_identity(tmp_path):
    source = form_fixture(tmp_path / "source.pdf")
    job = _run(tmp_path, "oxide_form_inspect", source)
    assert job.state == "completed", job.errors
    report = _report(job)
    fields = {field["name"]: field for field in report["fields"]}
    assert fields["account"]["value"] == "000123.40"
    assert fields["consent"]["value"] is False
    assert fields["notes"]["value"] == "Line one\nLine two"
    assert fields["locked"]["is_readonly"] is True
    assert fields["unsigned"]["field_type"] == "signature"
    assert report["has_xfa"] is False
    assert report["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert [page["page"] for page in report["pages"]] == [1, 2]
    widgets = [
        {a.get("field_name") for a in p["annotations"] if a["subtype"] == "Widget"}
        for p in report["pages"]
    ]
    assert "account" in widgets[0] and "notes" not in widgets[0]
    assert "notes" in widgets[1] and "account" not in widgets[1]


@native
def test_fill_reopens_exact_values_and_preserves_untouched_fields(tmp_path):
    source = form_fixture(tmp_path / "source.pdf")
    values = {"account": "001.20", "consent": True, "choice": "Beta", "notes": "A&B <x>\n002.50"}
    job = _run(tmp_path, "oxide_form_fill", source, {"values": json.dumps(values)})
    assert job.state == "completed", job.errors
    report = _report(job)
    assert report["saved_values_match"] is True
    assert report["visual_accuracy_verified"] is False
    actual = {field["name"]: field["value"] for field in report["fields_after"]}
    assert all(actual[name] == value for name, value in values.items())
    assert actual["locked"] == "Retain me"
    assert actual["short"] == "001"
    with pymupdf.open(_output(job, ".pdf")) as result:
        widgets = {
            widget.field_name: widget.field_value
            for page in result
            for widget in page.widgets() or []
        }
        assert widgets["account"] == "001.20"
        assert widgets["notes"] == "A&B <x>\n002.50"
        assert "Synthetic form page 2; total 20.15" in result[1].get_text()


@native
def test_fill_unicode_exact_values_are_separate_from_appearance_claims(tmp_path):
    source = form_fixture(tmp_path / "source.pdf")
    value = "Résumé Καλημέρα 日本語 001.25"
    job = _run(tmp_path, "oxide_form_fill", source, {"values": json.dumps({"account": value})})
    assert job.state == "completed", job.errors
    assert (
        next(field["value"] for field in _report(job)["fields_after"] if field["name"] == "account")
        == value
    )
    assert any("appearance" in warning for warning in _report(job)["warnings"])


@native
@pytest.mark.parametrize(
    "values",
    [
        {"missing": "x"},
        {"locked": "x"},
        {"unsigned": "x"},
        {"short": "0001"},
        {"account": True},
        {"choice": False},
    ],
)
def test_invalid_field_mutations_publish_nothing(tmp_path, values):
    source = form_fixture(tmp_path / "source.pdf")
    _assert_failed(_run(tmp_path, "oxide_form_fill", source, {"values": json.dumps(values)}))


@native
@pytest.mark.parametrize("export_format", ["fdf", "xfdf"])
def test_form_data_export_is_inert_and_exact(tmp_path, export_format):
    source = form_fixture(tmp_path / "source.pdf")
    filled = _run(
        tmp_path,
        "oxide_form_fill",
        source,
        {"values": json.dumps({"account": "A&B <001.20> (x) \\"})},
    )
    assert filled.state == "completed", filled.errors
    job = _run(tmp_path, "oxide_form_export", _output(filled, ".pdf"), {"format": export_format})
    assert job.state == "completed", job.errors
    data = _output(job, "." + export_format).read_bytes()
    assert _report(job)["export"]["sha256"] == hashlib.sha256(data).hexdigest()
    if export_format == "xfdf":
        # Parse only the known synthetic output, never arbitrary uploaded XML.
        root = ElementTree.fromstring(data)  # noqa: S314 -- generated fixture output, no user XML
        values = {f.attrib["name"]: f.findtext("{*}value") for f in root.findall(".//{*}field")}
        assert values["account"] == "A&B <001.20> (x) \\"
    else:
        assert data.startswith(b"%FDF-")
        assert b"001.20" in data


@native
def test_partial_form_flatten_preserves_other_page_widgets_and_notes(tmp_path):
    source = form_fixture(tmp_path / "source.pdf")
    job = _run(tmp_path, "oxide_form_flatten", source, {"pages": "1", "allow_flatten": True})
    assert job.state == "completed", job.errors
    report = _report(job)
    assert not any(a["subtype"] == "Widget" for a in report["pages_after"][0]["annotations"])
    with pymupdf.open(_output(job, ".pdf")) as output:
        assert not list(output[0].widgets() or [])
        assert {widget.field_name for widget in output[1].widgets()} == {"notes", "short"}
        assert any(
            annotation.info["content"] == "Keep note on page 1" for annotation in output[0].annots()
        )
    assert report["visual_accuracy_verified"] is False


@native
def test_whole_form_flatten_removes_acroform_and_retains_source_text(tmp_path):
    source = form_fixture(tmp_path / "source.pdf")
    job = _run(tmp_path, "oxide_form_flatten", source, {"allow_flatten": True})
    assert job.state == "completed", job.errors
    assert _report(job)["fields_after"] == []
    with pymupdf.open(_output(job, ".pdf")) as output:
        assert all(not list(page.widgets() or []) for page in output)
        assert all("total 20.15" in page.get_text() for page in output)


@native
def test_xfa_detection_is_explicit_and_edits_fail_closed(tmp_path):
    source = form_fixture(tmp_path / "source.pdf")
    xfa = tmp_path / "xfa.pdf"
    with pymupdf.open(source) as doc:
        stream = doc.get_new_xref()
        doc.update_object(stream, "<<>>")
        doc.update_stream(stream, b'<xdp xmlns="http://ns.adobe.com/xdp/">Synthetic XFA only</xdp>')
        doc.xref_set_key(doc.pdf_catalog(), "AcroForm/XFA", f"{stream} 0 R")
        doc.save(xfa)
    inspect = _run(tmp_path, "oxide_form_inspect", xfa)
    assert inspect.state == "completed", inspect.errors
    assert _report(inspect)["has_xfa"] is True
    assert _report(inspect)["warnings"]
    for operation, options in [
        ("oxide_form_fill", {"values": '{"account":"001.00"}'}),
        ("oxide_form_flatten", {"allow_flatten": True}),
        ("oxide_form_export", {}),
    ]:
        _assert_failed(_run(tmp_path, operation, xfa, options))


@native
def test_annotation_inspection_preserves_rectangles_and_page_order(tmp_path):
    source = annotation_fixture(tmp_path / "source.pdf")
    job = _run(tmp_path, "oxide_annotation_inspect", source, {"pages": "2,1"})
    assert job.state == "completed", job.errors
    report = _report(job)
    assert [page["page"] for page in report["pages"]] == [2, 1]
    for page in report["pages"]:
        link = next(a for a in page["annotations"] if a["subtype"] == "Link")
        assert link["rect"] == [40, 345, 180, 370]
        assert link["action_uri"] == f"https://example.invalid/source-{page['page']}"


@native
def test_annotation_add_reopens_geometry_contents_and_page_identity(tmp_path):
    source = annotation_fixture(tmp_path / "source.pdf")
    actions = [
        {"type": "note", "page": 2, "x": 150, "y": 200, "text": "Review <001.25> & 002"},
        {
            "type": "link",
            "page": 1,
            "x": 180,
            "y": 220,
            "width": 100,
            "height": 20,
            "url": "https://example.invalid/added?a=001&b=2",
        },
    ]
    job = _run(tmp_path, "oxide_annotation_edit", source, {"actions": json.dumps(actions)})
    assert job.state == "completed", job.errors
    report = _report(job)
    first, second = report["pages_after"]
    assert any(a.get("contents") == "Original note 1" for a in first["annotations"])
    assert report["saved_annotation_metadata_match"] is True
    assert report["native_page_text_unchanged"] is True
    assert any(
        a.get("action_uri") == actions[1]["url"] and a["rect"] == [180, 220, 280, 240]
        for a in first["annotations"]
    )
    assert any(
        a.get("contents") == actions[0]["text"] and a["rect"] == [150, 200, 174, 224]
        for a in second["annotations"]
    )
    assert any(a["subtype"] == "Highlight" for a in second["annotations"])
    with pymupdf.open(_output(job, ".pdf")) as output:
        assert all("Invoice 000123.40; total 20.15" in page.get_text() for page in output)


@native
def test_annotation_removal_persistence_defect_fails_closed(tmp_path):
    source = annotation_fixture(tmp_path / "source.pdf")
    job = _run(
        tmp_path,
        "oxide_annotation_edit",
        source,
        {"actions": json.dumps([{"type": "remove", "page": 1, "index": 0}])},
    )
    _assert_failed(job)
    assert "saved annotations differ" in str(job.errors)


@native
def test_added_highlight_appearance_defect_is_explicitly_gated(tmp_path):
    source = annotation_fixture(tmp_path / "source.pdf")
    action = {
        "type": "highlight",
        "page": 1,
        "x": 200,
        "y": 200,
        "width": 80,
        "height": 30,
        "color": [1, 0.8, 0],
    }
    with pytest.raises(JobError, match="appearance disappears in PDFium"):
        _run(tmp_path, "oxide_annotation_edit", source, {"actions": json.dumps([action])})
    assert not (tmp_path / "jobs").exists()


@native
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_explicit_highlight_correction_preserves_crop_rotation_text_and_annotations(
    tmp_path, rotation
):
    import pikepdf
    from PIL import ImageChops

    original = annotation_fixture(tmp_path / "original.pdf")
    source = tmp_path / "source.pdf"
    with pikepdf.open(original) as pdf:
        pdf.pages[0].MediaBox = pikepdf.Array([-40, -30, 360, 470])
        pdf.pages[0].CropBox = pikepdf.Array([-20, 0, 340, 465])
        pdf.pages[0].Rotate = rotation
        pdf.save(source)
    action = _highlight_action()
    over_numbers = {**action, "x": 150, "y": 435, "width": 180, "height": 22}
    job = _run(
        tmp_path,
        "oxide_annotation_edit",
        source,
        {"actions": json.dumps([action, over_numbers]), "allow_highlight_repair": True},
    )
    assert job.state == "completed", job.errors
    corrected = _output(job, "modified.pdf")
    native_output = _output(job, "native-unverified.pdf")
    report = _report(job)
    correction = report["highlight_correction"]
    assert correction["engine"] == "pikepdf"
    assert correction["explicit_opt_in"] is True
    assert correction["native_defect_fixed_upstream"] is False
    assert correction["source_annotation_graphs_unchanged"] is True
    assert correction["native_sha256"] == hashlib.sha256(native_output.read_bytes()).hexdigest()
    assert correction["corrected_sha256"] == hashlib.sha256(corrected.read_bytes()).hexdigest()
    assert report["visual_accuracy_verified"] is False
    with pikepdf.open(native_output) as raw, pikepdf.open(corrected) as saved:
        raw_annotation = raw.pages[0].Annots[-2]
        fixed = saved.pages[0].Annots[-2]
        assert raw_annotation.get("/AP") is None
        assert list(raw_annotation.QuadPoints) == [180, 200, 260, 200, 260, 230, 180, 230]
        assert list(fixed.QuadPoints) == [180, 230, 260, 230, 180, 200, 260, 200]
        assert list(fixed.AP.N.BBox) == [0, 0, 80, 30]
        assert str(fixed.AP.N.Resources.ExtGState.HighlightRepair.BM) == "/Multiply"
    baseline, baseline_text, bounds = _renderer_snapshot(source, 0, action)
    actual, actual_text, actual_bounds = _renderer_snapshot(corrected, 0, action)
    assert actual_bounds == bounds
    assert actual_text == baseline_text
    assert all("000123.40" in text and "20.15" in text for text in actual_text)
    raw_render, raw_text, _ = _renderer_snapshot(native_output, 0, action)
    assert raw_text == baseline_text
    # Compare complete interiors, not one pixel or a renderer success flag.
    left, top, right, bottom = bounds
    interior = (left + 3, top + 3, right - 3, bottom - 3)
    for engine in actual:
        pixels = list(actual[engine].crop(interior).get_flattened_data())
        assert pixels and all(
            red > 240 and 190 < green < 220 and blue < 20 for red, green, blue in pixels
        )
        # Every source black text pixel remains black, including invoice digits under the highlight.
        for before, after in zip(
            baseline[engine].get_flattened_data(), actual[engine].get_flattened_data(), strict=True
        ):
            if max(before) < 35:
                assert max(after) < 40
        baseline[engine].save(tmp_path / f"source-{engine}.png")
        actual[engine].save(tmp_path / f"corrected-{engine}.png")
        raw_render[engine].save(tmp_path / f"native-unverified-{engine}.png")
    assert (
        ImageChops.difference(raw_render["pdfium"], baseline["pdfium"]).crop(interior).getbbox()
        is None
    )
    original_second, second_text, _ = _renderer_snapshot(source, 1)
    corrected_second, corrected_second_text, _ = _renderer_snapshot(corrected, 1)
    assert second_text == corrected_second_text
    assert all(
        ImageChops.difference(original_second[engine], corrected_second[engine]).getbbox() is None
        for engine in original_second
    )


@native
@pytest.mark.parametrize("case", ["outside_crop", "user_unit", "removal"])
def test_highlight_correction_refuses_unsupported_geometry_and_other_native_failures(
    tmp_path, case
):
    import pikepdf

    original = annotation_fixture(tmp_path / "original.pdf")
    source = tmp_path / "source.pdf"
    with pikepdf.open(original) as pdf:
        if case == "outside_crop":
            pdf.pages[0].CropBox = pikepdf.Array([0, 0, 200, 500])
        if case == "user_unit":
            pdf.pages[0].UserUnit = 2
        pdf.save(source)
    actions = [_highlight_action()]
    if case == "removal":
        actions.append({"type": "remove", "page": 1, "index": 0})
    job = _run(
        tmp_path,
        "oxide_annotation_edit",
        source,
        {"actions": json.dumps(actions), "allow_highlight_repair": True},
    )
    _assert_failed(job)
    expected = {
        "outside_crop": "visible crop",
        "user_unit": "UserUnit 1",
        "removal": "saved annotations differ",
    }
    assert expected[case] in str(job.errors)


def test_highlight_repair_requires_explicit_highlight_and_minimum_dimensions():
    with pytest.raises(JobError, match="explicit new highlight"):
        validate_options(
            "oxide_annotation_edit",
            {
                "actions": '[{"type":"note","page":1,"x":1,"y":1,"text":"x"}]',
                "allow_highlight_repair": True,
            },
        )
    with pytest.raises(JobError, match=r"at least 0\.5"):
        validate_options(
            "oxide_annotation_edit",
            {
                "actions": json.dumps([{**_highlight_action(), "width": 0.1}]),
                "allow_highlight_repair": True,
            },
        )


@native
def test_highlight_correction_preserves_form_values_with_mixed_additions(tmp_path):
    source = form_fixture(tmp_path / "source.pdf")
    actions = [
        {"type": "note", "page": 2, "x": 310, "y": 250, "text": "Keep 000123.40"},
        {
            "type": "link",
            "page": 1,
            "x": 40,
            "y": 10,
            "width": 100,
            "height": 20,
            "url": "https://example.invalid/new?amount=001.20",
        },
        {**_highlight_action(), "x": 310, "y": 250, "width": 60},
    ]
    job = _run(
        tmp_path,
        "oxide_annotation_edit",
        source,
        {"actions": json.dumps(actions), "allow_highlight_repair": True},
    )
    assert job.state == "completed", job.errors
    with pymupdf.open(source) as before, pymupdf.open(_output(job, "modified.pdf")) as after:
        original = [
            (number, widget.field_name, widget.field_value, tuple(widget.rect))
            for number, page in enumerate(before)
            for widget in page.widgets()
        ]
        actual = [
            (number, widget.field_name, widget.field_value, tuple(widget.rect))
            for number, page in enumerate(after)
            for widget in page.widgets()
        ]
        assert actual == original
        assert next(value for _, name, value, _ in actual if name == "account") == "000123.40"
        assert next(value for _, name, value, _ in actual if name == "short") == "001"
        assert after[0].get_links()[0]["uri"] == actions[1]["url"]
    correction = _report(job)["highlight_correction"]
    assert len(correction["changes"]) == 1
    assert correction["changes"][0]["page"] == 1


@native
def test_highlight_diagnostic_and_candidate_share_atomic_output_budget(tmp_path):
    source = annotation_fixture(tmp_path / "source.pdf")
    options = {"actions": json.dumps([_highlight_action()]), "allow_highlight_repair": True}
    valid = _run(tmp_path, "oxide_annotation_edit", source, options)
    assert valid.state == "completed", valid.errors
    pdf_bytes = sum(
        Path(item["path"]).stat().st_size for item in valid.results if item["path"].endswith(".pdf")
    )
    rejected = _run(
        tmp_path,
        "oxide_annotation_edit",
        source,
        options,
        limits=Limits(max_output_bytes=pdf_bytes - 1),
    )
    _assert_failed(rejected)


@native
def test_highlight_correction_does_not_repair_unknown_native_shapes(tmp_path):
    import io

    import pikepdf

    from pdf_multitool.oxide_forms import _repair_native_highlights

    source = annotation_fixture(tmp_path / "source.pdf")
    action = _highlight_action()
    job = _run(
        tmp_path,
        "oxide_annotation_edit",
        source,
        {"actions": json.dumps([action]), "allow_highlight_repair": True},
    )
    assert job.state == "completed", job.errors
    with pikepdf.open(_output(job, "native-unverified.pdf")) as pdf:
        pdf.pages[0].Annots[-1].QuadPoints[0] = 190
        malformed = io.BytesIO()
        pdf.save(malformed)
    with pytest.raises(JobError, match="known native defect"):
        _repair_native_highlights(
            source.read_bytes(), malformed.getvalue(), [action], _report(job)["pages_before"]
        )


@native
def test_encrypted_rewrite_preserves_security_by_failing_closed(tmp_path):
    original = form_fixture(tmp_path / "source.pdf")
    encrypted = tmp_path / "encrypted.pdf"
    password = "Synthetic-forms-test-001"  # noqa: S105 -- synthetic fixture only
    with pymupdf.open(original) as doc:
        doc.save(
            encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password
        )
    job = _run(
        tmp_path,
        "oxide_form_fill",
        encrypted,
        {"values": '{"account":"001.00"}', "password": password},
    )
    _assert_failed(job)
    assert "encrypted input protections" in str(job.errors)


@native
def test_unicode_fdf_is_rejected_and_xfdf_keeps_exact_unicode(tmp_path):
    source = form_fixture(tmp_path / "source.pdf")
    value = "Résumé 日本語 0001.25"
    filled = _run(tmp_path, "oxide_form_fill", source, {"values": json.dumps({"account": value})})
    assert filled.state == "completed", filled.errors
    pdf = _output(filled, ".pdf")
    fdf = _run(tmp_path, "oxide_form_export", pdf, {"format": "fdf"})
    _assert_failed(fdf)
    assert "FDF Unicode encoding" in str(fdf.errors)
    xfdf = _run(tmp_path, "oxide_form_export", pdf, {"format": "xfdf"})
    assert xfdf.state == "completed", xfdf.errors
    assert value in _output(xfdf, ".xfdf").read_text()


@native
def test_multi_choice_values_preserved_but_lossy_xfdf_rejected(tmp_path):
    source = tmp_path / "multi.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        widget = pymupdf.Widget()
        widget.field_name = "choices"
        widget.field_type = pymupdf.PDF_WIDGET_TYPE_LISTBOX
        widget.field_flags = 1 << 21
        widget.choice_values = ["A,B", "C", "D"]
        widget.rect = pymupdf.Rect(40, 40, 240, 120)
        page.add_widget(widget)
        doc.save(source)
    filled = _run(
        tmp_path, "oxide_form_fill", source, {"values": json.dumps({"choices": ["A,B", "C"]})}
    )
    assert filled.state == "completed", filled.errors
    assert _report(filled)["fields_after"][0]["value"] == ["A,B", "C"]
    xfdf = _run(tmp_path, "oxide_form_export", _output(filled, ".pdf"), {"format": "xfdf"})
    _assert_failed(xfdf)
    assert "array/empty-string distinctions" in str(xfdf.errors)
    fdf = _run(tmp_path, "oxide_form_export", _output(filled, ".pdf"), {"format": "fdf"})
    assert fdf.state == "completed", fdf.errors
    assert b"[ (A,B) (C) ]" in _output(fdf, ".fdf").read_bytes()


@native
def test_invalid_choice_radio_and_custom_checkbox_states_fail_closed(tmp_path):
    source = form_fixture(tmp_path / "source.pdf")
    _assert_failed(_run(tmp_path, "oxide_form_fill", source, {"values": '{"choice":"Gamma"}'}))
    for kind in ("radio", "custom"):
        path = tmp_path / f"{kind}.pdf"
        with pymupdf.open(source) as doc:
            checkbox = next(widget for widget in doc[0].widgets() if widget.field_name == "consent")
            if kind == "radio":
                doc.xref_set_key(checkbox.xref, "Ff", "32768")
            else:
                appearance = doc.xref_get_key(checkbox.xref, "AP/N/Yes")[1]
                doc.xref_set_key(checkbox.xref, "AP/N/Custom", appearance)
                doc.xref_set_key(checkbox.xref, "AP/N/Yes", "null")
            doc.save(path)
        job = _run(tmp_path, "oxide_form_fill", path, {"values": '{"consent":true}'})
        _assert_failed(job)


@native
def test_empty_string_xfdf_export_is_explicitly_unsupported(tmp_path):
    source = form_fixture(tmp_path / "source.pdf")
    job = _run(tmp_path, "oxide_form_fill", source, {"values": '{"account":""}'})
    assert job.state == "completed", job.errors
    assert (
        next(field["value"] for field in _report(job)["fields_after"] if field["name"] == "account")
        == ""
    )
    exported = _run(tmp_path, "oxide_form_export", _output(job, ".pdf"), {"format": "xfdf"})
    _assert_failed(exported)
    assert "array/empty-string distinctions" in str(exported.errors)


@native
def test_annotation_flatten_page_selection_and_widget_gate(tmp_path):
    source = annotation_fixture(tmp_path / "source.pdf")
    job = _run(tmp_path, "oxide_annotation_flatten", source, {"pages": "1", "allow_flatten": True})
    assert job.state == "completed", job.errors
    report = _report(job)
    assert report["marked_pages"] == [1]
    assert report["pages_after"][0]["annotations"] == []
    with pymupdf.open(_output(job, ".pdf")) as output:
        assert not list(output[0].annots() or [])
        assert output[0].get_links() == []
        assert list(output[1].annots())
        assert output[1].get_links()
    from PIL import ImageChops

    original, original_text, _ = _renderer_snapshot(source, 1)
    saved, saved_text, _ = _renderer_snapshot(_output(job, ".pdf"), 1)
    assert original_text == saved_text
    assert all(
        ImageChops.difference(original[engine], saved[engine]).getbbox() is None
        for engine in original
    )
    assert report["independent_flatten_verification"]["saved_annotation_counts"] == [0, 4]
    form = form_fixture(tmp_path / "form.pdf")
    _assert_failed(_run(tmp_path, "oxide_annotation_flatten", form, {"allow_flatten": True}))


@native
@pytest.mark.parametrize("mutation", ["annotation_loss", "resource_change"])
def test_flatten_verification_rejects_unselected_page_changes(tmp_path, mutation):
    from pdf_multitool.oxide_forms import _verify_flatten_saved

    source = annotation_fixture(tmp_path / "source.pdf")
    job = _run(tmp_path, "oxide_annotation_flatten", source, {"pages": "1", "allow_flatten": True})
    assert job.state == "completed", job.errors
    with pymupdf.open(_output(job, ".pdf")) as altered:
        page = altered[1]
        if mutation == "annotation_loss":
            page.delete_annot(next(page.annots()))
        else:
            font_xref = page.get_fonts()[0][0]
            altered.xref_set_key(font_xref, "BaseFont", "/Courier")
        tampered = altered.tobytes()
    with pytest.raises(JobError, match="unselected page"):
        _verify_flatten_saved(source.read_bytes(), tampered, [0])


@native
@pytest.mark.parametrize("pages", ["1", "1,2"])
def test_link_only_flatten_rejects_persisting_interactivity(tmp_path, pages):
    source = tmp_path / "link-only.pdf"
    with pymupdf.open() as doc:
        for number in range(2):
            page = doc.new_page(width=400, height=500)
            page.insert_text((40, 60), f"Invoice 000123.40; total 20.15; page {number + 1}")
            page.insert_link(
                {
                    "kind": pymupdf.LINK_URI,
                    "from": pymupdf.Rect(40, 80, 180, 100),
                    "uri": f"https://example.invalid/keep-{number + 1}",
                }
            )
        doc.save(source)
    job = _run(
        tmp_path, "oxide_annotation_flatten", source, {"pages": pages, "allow_flatten": True}
    )
    _assert_failed(job)
    assert "annotations remain" in str(job.errors)


@native
def test_no_appearance_highlight_flatten_rejects_persisting_annotation(tmp_path):
    source = tmp_path / "no-appearance.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=500)
        page.insert_text((40, 60), "Invoice 000123.40; total 20.15")
        annotation = page.add_highlight_annot(pymupdf.Rect(40, 45, 180, 65))
        doc.xref_set_key(annotation.xref, "AP", "null")
        doc.save(source)
    job = _run(tmp_path, "oxide_annotation_flatten", source, {"allow_flatten": True})
    _assert_failed(job)
    assert "annotations remain" in str(job.errors)


@native
def test_blank_field_loss_warning_rejects_form_flatten(tmp_path):
    source = tmp_path / "missing-signature-appearance.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=500)
        page.insert_text((40, 60), "Unsigned synthetic form; amount 000123.40")
        widget = pymupdf.Widget()
        widget.field_name = "signature-without-appearance"
        widget.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
        widget.rect = pymupdf.Rect(40, 80, 250, 120)
        page.add_widget(widget)
        doc.xref_set_key(next(page.widgets()).xref, "AP", "null")
        doc.save(source)
    job = _run(tmp_path, "oxide_form_flatten", source, {"allow_flatten": True})
    _assert_failed(job)
    assert "blank rectangle" in str(job.errors)
    assert "signature-without-appearance" in str(job.errors)


@pytest.mark.parametrize(
    "values", ['{"x":1}', '{"x":"a","x":"b"}', "[]", "{}", '{"x":NaN}', '{"x":{"nested":true}}']
)
def test_form_config_validation_rejects_invalid_values(values):
    with pytest.raises(JobError):
        validate_options("oxide_form_fill", {"values": values})


@pytest.mark.parametrize(
    "action",
    [
        {
            "type": "link",
            "page": 1,
            "x": 1,
            "y": 2,
            "width": 10,
            "height": 10,
            "url": "javascript:alert(1)",
        },
        {
            "type": "link",
            "page": 1,
            "x": 1,
            "y": 2,
            "width": 10,
            "height": 10,
            "url": "file:///etc/passwd",
        },
        {
            "type": "link",
            "page": 1,
            "x": 1,
            "y": 2,
            "width": 10,
            "height": 10,
            "url": "https://user:pass@example.invalid/",
        },
        {"type": "note", "page": 0, "x": 1, "y": 2, "text": "x"},
        {"type": "note", "page": 1, "x": 1, "y": 2, "text": "x", "path": "outside"},
        {
            "type": "highlight",
            "page": 1,
            "x": 0,
            "y": 0,
            "width": -1,
            "height": 10,
            "color": [1, 0, 0],
        },
        {"type": "run_javascript", "page": 1, "script": "alert(1)"},
    ],
)
def test_annotation_action_allowlist_rejects_unsafe_or_invalid_input(action):
    with pytest.raises(JobError):
        validate_options("oxide_annotation_edit", {"actions": json.dumps([action])})


@pytest.mark.parametrize("operation", ["oxide_form_flatten", "oxide_annotation_flatten"])
def test_flatten_requires_explicit_operation_option(operation):
    with pytest.raises(JobError, match="allow_flatten"):
        validate_options(operation, {"allow_flatten": False})


@native
def test_form_output_quota_failure_removes_partial_files(tmp_path):
    source = form_fixture(tmp_path / "source.pdf")
    _assert_failed(
        _run(tmp_path, "oxide_form_inspect", source, limits=Limits(max_output_bytes=256))
    )
