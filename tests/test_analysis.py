"""Synthetic-only development tests; no user documents or network access.

Runs against the available PyMuPDF engine. Engine installation provenance is not
verified by these tests and successful tests do not establish upstream integrity.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pymupdf

from pdf_multitool.analysis import (
    _render_line,
    analyze_document,
    export_html,
    export_markdown,
    export_text,
)


class HTMLInspection(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.ids = []
        self.links = []
        self.images = []
        self.tags = []
        self.attributes = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append(tag)
        self.attributes.append(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        if tag == "img":
            self.images.append(attrs.get("src", ""))


def image_bytes():
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 24, 24), False)
    pix.clear_with(150)
    return pix.tobytes("png")


def body(page, y=130):
    page.insert_textbox(
        pymupdf.Rect(60, y, 535, y + 180),
        "This document contains ordinary body text that describes a synthetic example. "
        "It includes enough real text to distinguish the title from paragraph typography. "
        "All content is invented for local tests and includes no personal information.",
        fontsize=11,
    )


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.doc = pymupdf.open()
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "result.html"

    def tearDown(self):
        self.doc.close()
        self.temp.cleanup()

    def export(self, name="original.pdf", options=None):
        result = export_html(self.doc, self.output, name, options)
        source = self.output.read_text(encoding="utf-8")
        return result, source, HTMLInspection(source)

    def test_prominent_title_and_font_counts(self):
        page = self.doc.new_page()
        page.insert_text((60, 85), "Quarterly Operations Review", fontsize=26, fontname="hebo")
        body(page)
        self.doc.set_metadata({"title": "Quarterly Operations Review"})
        result = analyze_document(self.doc, "scan001.pdf")
        self.assertEqual(result["suggested_filename"], "Quarterly Operations Review.pdf")
        self.assertEqual(result["naming"]["confidence"], "high")
        self.assertEqual(result["naming"]["source"], "metadata_and_prominent_title")
        self.assertFalse(result["naming"]["preserve_original"])
        self.assertEqual(
            sum(f["character_count"] for f in result["fonts"]), result["text_layer_characters"]
        )
        self.assertEqual(sum(f["span_count"] for f in result["fonts"]), result["text_span_count"])
        self.assertTrue(any(f["bold"] for f in result["fonts"]))
        self.assertEqual(result["pages"][0]["text_kind"], "text_layer")
        json.dumps(result, allow_nan=False)

    def test_metadata_alone_does_not_rename(self):
        page = self.doc.new_page()
        body(page)
        self.doc.set_metadata({"title": "This Metadata Is Unrelated"})
        result = analyze_document(self.doc, "Meaningful original name.pdf")
        self.assertEqual(result["suggested_filename"], "Meaningful original name.pdf")
        self.assertTrue(result["naming"]["preserve_original"])

    def test_competing_large_titles_preserve_original(self):
        page = self.doc.new_page()
        page.insert_text((60, 65), "First Possible Document Title", fontsize=24)
        page.insert_text((60, 100), "Second Possible Document Title", fontsize=24)
        body(page, 145)
        result = analyze_document(self.doc, "original.pdf")
        self.assertTrue(result["naming"]["preserve_original"])

    def test_dominant_title_without_metadata(self):
        page = self.doc.new_page()
        page.insert_text((60, 70), "Synthetic Operations Handbook", fontsize=27)
        body(page)
        result = analyze_document(self.doc, "original.pdf")
        self.assertEqual(result["naming"]["source"], "dominant_first_page_title")

    def test_page_dimensions_rotation_and_scan_candidates(self):
        page = self.doc.new_page(width=400, height=600)
        page.insert_image(page.rect, stream=image_bytes())
        page.set_rotation(90)
        self.doc.new_page()
        result = analyze_document(self.doc, "scan.pdf")
        scan = result["pages"][0]
        self.assertEqual(scan["rotation"], 90)
        self.assertEqual(scan["width_points"], 400)
        self.assertEqual(scan["height_points"], 600)
        self.assertEqual(scan["display_width_points"], 600)
        self.assertEqual(scan["display_height_points"], 400)
        self.assertTrue(scan["ocr_candidate"])
        self.assertEqual(result["ocr_candidate_pages"], [1])
        self.assertEqual(result["pages"][1]["text_kind"], "no_text_detected")
        self.assertFalse(result["pages"][1]["ocr_candidate"])

    def test_repeated_margins_are_candidates_and_retained(self):
        for number in range(1, 4):
            page = self.doc.new_page()
            page.insert_text((60, 38), "Confidential synthetic guide", fontsize=9)
            page.insert_text((260, 810), f"Page {number} of 3", fontsize=9)
            body(page)
        report, source, _ = self.export()
        candidates = report["analysis"]["repeated_header_footer_candidates"]
        self.assertEqual({c["position"] for c in candidates}, {"header", "footer"})
        self.assertEqual([1, 2, 3], candidates[0]["pages"])
        self.assertEqual(source.count("Confidential synthetic guide"), 3)
        self.assertIn("Page 2 of 3", source)
        self.assertIn("margin-candidate", source)

    def test_two_pages_not_enough_for_repeated_margin(self):
        for _ in range(2):
            page = self.doc.new_page()
            page.insert_text((60, 38), "Shared short header", fontsize=10)
            body(page)
        self.assertEqual(
            analyze_document(self.doc, "test.pdf")["repeated_header_footer_candidates"], []
        )

    def test_self_contained_images_and_all_anchors_resolve(self):
        page = self.doc.new_page()
        page.insert_text((60, 65), "A Searchable Document Title", fontsize=24)
        body(page)
        page.insert_image(pymupdf.Rect(60, 360, 150, 450), stream=image_bytes())
        self.doc.set_toc(
            [[1, "A Searchable Document Title", 1], [2, "Bookmark without matching text", 1]]
        )
        result, source, parsed = self.export()
        self.assertTrue(result["self_contained"])
        self.assertEqual(result["pages_exported"], 1)
        self.assertEqual(result["images_embedded"], 1)
        self.assertEqual(len(parsed.images), 1)
        self.assertTrue(parsed.images[0].startswith("data:image/png;base64,"))
        decoded = base64.b64decode(parsed.images[0].split(",", 1)[1])
        self.assertEqual(pymupdf.Pixmap(decoded).width, 24)
        self.assertEqual(len(parsed.ids), len(set(parsed.ids)))
        for link in parsed.links:
            self.assertTrue(link.startswith("#"))
            self.assertIn(link[1:], parsed.ids)
        self.assertEqual(result["heading_links"], len(result["analysis"]["heading_candidates"]))
        self.assertIn("#page-1", parsed.links)
        self.assertIn("Source page 1", source)
        self.assertEqual(result["outputs"][0]["path"], str(self.output))
        self.assertNotIn("script", parsed.tags)
        self.assertNotIn("iframe", parsed.tags)
        self.assertIn("Content-Security-Policy", source)
        self.assertIn("overflow-wrap:anywhere", source)
        self.assertIn("@media", source)
        self.assertEqual(
            result["text_characters_exported"], result["analysis"]["text_layer_characters"]
        )
        self.assertEqual(set(Path(self.temp.name).iterdir()), {self.output})
        json.dumps(result, allow_nan=False)

    def test_escape_text_metadata_and_outline(self):
        page = self.doc.new_page()
        page.insert_text((60, 80), '<script>alert("x")</script> & text', fontsize=12)
        self.doc.set_toc([[1, '<img src=x onerror="alert(1)">', 1]])
        _, source, parsed = self.export('<evil onclick="x">.pdf')
        self.assertNotIn("script", parsed.tags)
        self.assertNotIn("img", parsed.tags)
        self.assertNotIn("evil", parsed.tags)
        self.assertIn("&lt;script&gt;", source)
        self.assertIn("&amp; text", source)
        for attrs in parsed.attributes:
            self.assertFalse(any(k.startswith("on") for k in attrs))

    def test_typography_geometry_infers_subscript(self):
        page = self.doc.new_page()
        page.insert_text((60, 100), "H", fontsize=12)
        page.insert_text((68, 104), "2", fontsize=8)
        page.insert_text((73, 100), "O", fontsize=12)
        page.insert_text((60, 150), "Bold and italic", fontsize=12, fontname="hebi")
        report, source, parsed = self.export()
        self.assertIn("sub", parsed.tags)
        self.assertGreater(report["geometry_inferred_script_spans"], 0)
        self.assertIn("font-weight:700", source)
        self.assertIn("font-style:italic", source)

    def test_superscript_flag_and_light_text_reporting(self):
        from collections import Counter

        stats = Counter()
        line = {
            "dir": (1, 0),
            "spans": [
                {
                    "text": "2",
                    "font": 'Font" onload="bad',
                    "size": 8,
                    "flags": 1,
                    "color": 0xFFFFFF,
                    "origin": (10, 20),
                    "bbox": (10, 10, 20, 20),
                }
            ],
        }
        rendered = _render_line(line, 11, stats)
        self.assertIn("<sup", rendered)
        self.assertEqual(stats["superscript_flag_spans"], 1)
        self.assertEqual(stats["light_text_color_adjusted_spans"], 1)
        parsed = HTMLInspection(rendered)
        self.assertFalse(any("onload" in a for a in parsed.attributes))

    def test_geometry_reading_order_keeps_columns_together(self):
        page = self.doc.new_page()
        # Intentionally insert in alternating row order; clear gutters define columns.
        page.insert_text((55, 90), "LEFT FIRST", fontsize=11)
        page.insert_text((330, 90), "RIGHT FIRST", fontsize=11)
        page.insert_text((55, 200), "LEFT SECOND", fontsize=11)
        page.insert_text((330, 200), "RIGHT SECOND", fontsize=11)
        _, source, _ = self.export()
        self.assertLess(source.index("LEFT SECOND"), source.index("RIGHT FIRST"))

    def test_images_can_be_explicitly_excluded(self):
        page = self.doc.new_page()
        page.insert_image(page.rect, stream=image_bytes())
        report, source, parsed = self.export(options={"include_images": False})
        self.assertEqual(report["images_embedded"], 0)
        self.assertEqual(parsed.images, [])
        self.assertEqual(report["analysis"]["image_occurrences"], 1)
        self.assertIn("excluded", " ".join(report["warnings"]))
        self.assertNotIn("Raster images are included", source)

    def test_invalid_options_rejected(self):
        self.doc.new_page()
        with self.assertRaises(ValueError):
            self.export(options={"include_images": "false"})

    def test_export_does_not_mutate_document(self):
        page = self.doc.new_page()
        body(page)
        before_text = page.get_text()
        before_metadata = dict(self.doc.metadata)
        before_rotation = page.rotation
        self.export()
        self.assertEqual(before_text, self.doc[0].get_text())
        self.assertEqual(before_metadata, self.doc.metadata)
        self.assertEqual(before_rotation, self.doc[0].rotation)


class StructuredExportTests(unittest.TestCase):
    def setUp(self):
        self.doc = pymupdf.open()
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.doc.close()
        self.temp.cleanup()

    def export(self, markdown=True, options=None, filename=None, original="source.pdf"):
        function = export_markdown if markdown else export_text
        path = Path(self.temp.name) / (filename or ("document.md" if markdown else "document.txt"))
        report = function(self.doc, path, original, options)
        return report, path.read_text(encoding="utf-8"), path

    def test_real_unicode_characters_remain_source_characters(self):
        page = self.doc.new_page()
        font = pymupdf.Font("cjk")
        page.insert_font(fontname="unicode", fontbuffer=font.buffer)
        original = "αβΓ ± x² H₂O → café naïve"
        page.insert_text((60, 90), original, fontsize=12, fontname="unicode")
        self.assertIn(original, page.get_text())
        for markdown in (True, False):
            report, source, _ = self.export(markdown)
            self.assertIn(original, source)
            self.assertEqual(report["text_characters_exported"], len(original))
            self.assertEqual(
                report["text_characters_exported"], report["analysis"]["text_layer_characters"]
            )
            self.assertNotIn("\\alpha", source)
            self.assertIn("not verified character-complete", " ".join(report["warnings"]))
            json.dumps(report, allow_nan=False)

    def test_markdown_style_and_script_use_source_spans(self):
        page = self.doc.new_page()
        page.insert_text((60, 100), "H", fontsize=12)
        page.insert_text((68, 104), "2", fontsize=8)
        page.insert_text((73, 100), "O", fontsize=12)
        page.insert_text((60, 150), "Bold and italic", fontsize=12, fontname="hebi")
        report, source, _ = self.export()
        self.assertIn("<sub>2</sub>", source)
        self.assertIn("***Bold and italic***", source)
        self.assertGreater(report["geometry_inferred_script_spans"], 0)
        text_report, text, _ = self.export(False)
        self.assertIn("H2O", text)
        self.assertNotIn("H₂O", text)  # Do not substitute invented Unicode characters.
        self.assertNotIn("<sub>", text)
        self.assertIn("cannot represent font style", " ".join(text_report["warnings"]))

    def test_explicit_superscript_and_literal_equation_spans(self):
        from collections import Counter

        from pdf_multitool.analysis import _render_structured_line

        line = {
            "dir": (1, 0),
            "spans": [
                {"text": "x", "size": 12, "flags": 0, "origin": (10, 20), "bbox": (10, 8, 16, 20)},
                {"text": "2", "size": 8, "flags": 1, "origin": (16, 15), "bbox": (16, 7, 20, 15)},
            ],
        }
        stats = Counter()
        self.assertEqual(_render_structured_line(line, stats, True), "x<sup>2</sup>")
        self.assertEqual(stats["superscript_flag_spans"], 1)
        self.assertEqual(_render_structured_line(line, Counter(), False), "x2")

    def test_markdown_escapes_source_syntax_not_source_meaning(self):
        page = self.doc.new_page()
        literal = (
            "[link](https://example.com) ![image](x) <script>x</script> *bold* #tag $x^2$ &amp;"
        )
        page.insert_text((30, 90), literal, fontsize=8)
        _, source, _ = self.export()
        self.assertIn(r"\[link\]\(https://example\.com\)", source)
        self.assertIn(r"\!\[image\]\(x\)", source)
        self.assertIn(r"\<script\>x\</script\>", source)
        self.assertIn(r"\*bold\*", source)
        self.assertIn(r"\$x\^2\$", source)
        self.assertIn("&amp;amp;", source)
        self.assertNotIn("<script>", source)
        self.assertNotIn("![image](x)", source)
        _, text, _ = self.export(False)
        self.assertIn(literal, text)

    def test_styled_markdown_source_punctuation_is_literal(self):
        from pdf_multitool.analysis import _render_structured_line

        source = (
            "![Click](https://example.invalid/private.png) "
            "[local](file:///etc/passwd) [js](javascript:alert(1)) <img "
            'src="x"> &amp; `code`'
        )
        for flags in (1, 8, 17, 9, 27):
            with self.subTest(flags=flags):
                line = {
                    "dir": (1, 0),
                    "spans": [
                        {
                            "text": source,
                            "size": 11,
                            "flags": flags,
                            "origin": (10, 20),
                            "bbox": (10, 9, 500, 20),
                        }
                    ],
                }
                rendered = _render_structured_line(line, Counter(), True)
                self.assertNotIn("![Click]", rendered)
                self.assertNotIn("[local]", rendered)
                self.assertNotIn("<img src=", rendered)
                self.assertIn("&#33;&#91;Click&#93;", rendered)

    @unittest.skipUnless(
        shutil.which("pandoc"), "Optional preinstalled Pandoc renderer unavailable"
    )
    def test_commonmark_renderer_cannot_activate_styled_source_links(self):
        from pdf_multitool.analysis import _render_structured_line

        source = (
            "![Click](https://example.invalid/private.png) "
            "[local](file:///etc/passwd) [js](javascript:alert(1)) <img "
            'src="x"> &amp; `code`'
        )

        class RenderedText(HTMLParser):
            def __init__(self, html):
                super().__init__(convert_charrefs=True)
                self.parts = []
                self.feed(html)

            def handle_data(self, data):
                self.parts.append(data)

        for flags in (0, 1, 8, 17, 9, 27):
            with self.subTest(flags=flags):
                line = {
                    "dir": (1, 0),
                    "spans": [
                        {
                            "text": source,
                            "size": 11,
                            "flags": flags,
                            "origin": (10, 20),
                            "bbox": (10, 9, 500, 20),
                        }
                    ],
                }
                markdown = _render_structured_line(line, Counter(), True)
                result = subprocess.run(  # noqa: S603 -- fixed engine/CLI argv; no shell
                    [
                        shutil.which("pandoc"),
                        "--from=commonmark",
                        "--to=html5",
                        "--wrap=none",
                        "--sandbox",
                    ],
                    input=markdown,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=True,
                )
                inspection = HTMLInspection(result.stdout)
                self.assertEqual(inspection.images, [])
                self.assertEqual(inspection.links, [])
                self.assertNotIn("script", inspection.tags)
                self.assertEqual("".join(RenderedText(result.stdout).parts).strip(), source)

    def test_images_are_safe_relative_assets_and_manifest_is_complete(self):
        page = self.doc.new_page()
        page.insert_image(pymupdf.Rect(60, 120, 160, 220), stream=image_bytes())
        page.insert_text((60, 90), "Image source", fontsize=12)
        report, source, path = self.export(
            filename=".. odd [name].md", original="../../malicious.pdf"
        )
        links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", source)
        self.assertEqual(len(links), 1)
        self.assertNotIn("base64,", source)
        asset = path.parent / links[0]
        self.assertTrue(asset.is_file())
        self.assertEqual(pymupdf.Pixmap(str(asset)).width, 24)
        self.assertRegex(links[0], r"^[A-Za-z0-9_-]+/page-0001-image-0001\.(png|jpg)$")
        self.assertFalse(report["self_contained"])
        self.assertEqual(report["images_exported"], 1)
        self.assertEqual(report["outputs"][1]["name"], links[0])
        self.assertEqual(Path(report["outputs"][1]["path"]), asset)
        self.assertEqual(
            {Path(o["path"]) for o in report["outputs"]},
            {p for p in path.parent.rglob("*") if p.is_file()},
        )
        # Re-export gets a separate safe asset folder rather than clobbering assets.
        second, second_source, _ = self.export(filename=path.name)
        self.assertNotEqual(second["outputs"][1]["path"], report["outputs"][1]["path"])
        self.assertTrue(asset.is_file())
        self.assertIn(second["outputs"][1]["name"], second_source)

    def test_no_images_or_hidden_asset_folder_when_excluded(self):
        page = self.doc.new_page()
        page.insert_image(page.rect, stream=image_bytes())
        report, source, path = self.export(options={"include_images": False})
        self.assertEqual(report["images_exported"], 0)
        self.assertEqual(len(report["outputs"]), 1)
        self.assertNotIn("![", source)
        self.assertEqual(set(path.parent.iterdir()), {path})
        self.assertIn("excluded", " ".join(report["warnings"]))
        text_report, text, _ = self.export(False)
        self.assertEqual(text, "[Source page 1]\n\n")
        self.assertEqual(text_report["analysis"]["ocr_candidate_pages"], [1])
        self.assertIn("not transcribed", " ".join(text_report["warnings"]))

    def test_headings_pages_and_outline_only_anchors_resolve(self):
        page = self.doc.new_page()
        page.insert_text((60, 85), "Section One", fontsize=20)
        body(page)
        page = self.doc.new_page()
        page.insert_text((60, 85), "Subsection Two", fontsize=18)
        body(page)
        self.doc.set_toc([[1, "Section One", 1], [2, "Outline only", 2], [3, "Subsection Two", 2]])
        report, source, _ = self.export()
        anchors = re.findall(r'<a id="([^\"]+)"></a>', source)
        links = re.findall(r"\]\(#([^)]+)\)", source)
        self.assertEqual(len(anchors), len(set(anchors)))
        self.assertTrue(set(links).issubset(set(anchors)))
        self.assertIn("# Section One\n", source)
        self.assertIn("### Subsection Two\n", source)
        self.assertEqual(source.count("Outline only"), 1)  # Navigation only, no invented paragraph.
        self.assertEqual(report["heading_links"], 3)
        self.assertEqual(report["source_page_links"], 2)
        self.assertEqual(
            report["text_characters_exported"], report["analysis"]["text_layer_characters"]
        )

    def test_pdf_page_labels_remain_distinct_from_source_page_positions(self):
        self.doc.new_page()
        self.doc.new_page()
        self.doc.set_page_labels(
            [{"startpage": 0, "prefix": "Preface ", "style": "r", "firstpagenum": 1}]
        )
        for markdown in (True, False):
            report, source, _ = self.export(markdown)
            self.assertEqual(report["analysis"]["pages"][0]["page_label"], "Preface i")
            self.assertIn("Source page 1", source)
            self.assertIn("PDF label: Preface i", source)
            self.assertIn("PDF label: Preface ii", source)
        html_path = Path(self.temp.name) / "labels.html"
        export_html(self.doc, html_path, "original.pdf")
        html = html_path.read_text(encoding="utf-8")
        self.assertIn("Source page 1 (PDF label: Preface i)", html)
        self.assertIn("#page-2", HTMLInspection(html).links)

    def test_two_column_order_and_source_page_boundaries(self):
        for n in (1, 2):
            page = self.doc.new_page()
            page.insert_text((55, 90), f"LEFT FIRST P{n}", fontsize=11)
            page.insert_text((330, 90), f"RIGHT FIRST P{n}", fontsize=11)
            page.insert_text((55, 200), f"LEFT SECOND P{n}", fontsize=11)
            page.insert_text((330, 200), f"RIGHT SECOND P{n}", fontsize=11)
        for markdown in (True, False):
            _, source, _ = self.export(markdown)
            self.assertLess(source.index("LEFT SECOND P1"), source.index("RIGHT FIRST P1"))
            self.assertLess(source.index("RIGHT SECOND P1"), source.index("LEFT FIRST P2"))
            self.assertRegex(source, r"RIGHT SECOND P1[\s\S]*Source page 2[\s\S]*LEFT FIRST P2")

    def test_clear_paragraph_wrapping_joined_without_dehyphenation(self):
        page = self.doc.new_page()
        text = (
            "A continuous paragraph contains source words and enough content to wrap onto multiple "
            "lines within a single left aligned text block. This text should retain its words and "
            "punctuation while conservative geometric wrapping is cleaned."
        )
        page.insert_textbox(pymupdf.Rect(60, 90, 350, 260), text, fontsize=11)
        report, source, _ = self.export(False)
        self.assertGreater(report["paragraph_line_joins"], 1)
        self.assertIn(text, source)
        self.assertEqual(
            report["text_characters_exported"], report["analysis"]["text_layer_characters"]
        )
        from pdf_multitool.analysis import _paragraph_continues

        previous = {
            "dir": (1, 0),
            "bbox": (60, 100, 300, 112),
            "spans": [{"text": "keep this hyphen-", "size": 11}],
        }
        current = {
            "dir": (1, 0),
            "bbox": (60, 113, 300, 125),
            "spans": [{"text": "ated source", "size": 11}],
        }
        self.assertFalse(_paragraph_continues(previous, current, {"bbox": (60, 100, 300, 125)}))
        previous["spans"][0]["text"] = "x = y + z"
        self.assertFalse(_paragraph_continues(previous, current, {"bbox": (60, 100, 300, 125)}))
        previous["spans"][0]["text"] = "1. List item stays separate"
        self.assertFalse(_paragraph_continues(previous, current, {"bbox": (60, 100, 300, 125)}))

    def test_repeated_margins_retained_and_opt_out_is_counted(self):
        for n in range(1, 4):
            page = self.doc.new_page()
            page.insert_text((60, 38), "Repeated synthetic header", fontsize=9)
            page.insert_text((260, 810), f"Page {n} of 3", fontsize=9)
            body(page)
        for markdown in (True, False):
            retained, source, _ = self.export(markdown)
            self.assertEqual(source.count("Repeated synthetic header"), 3)
            self.assertEqual(retained["repeated_margin_lines_skipped"], 0)
            skipped, source, _ = self.export(markdown, {"skip_repeated_margins": True})
            self.assertNotIn("Repeated synthetic header", source)
            self.assertEqual(skipped["repeated_margin_lines_skipped"], 6)
            self.assertEqual(
                skipped["text_characters_exported"] + skipped["text_characters_skipped"],
                skipped["analysis"]["text_layer_characters"],
            )
            self.assertIn("false positives", " ".join(skipped["warnings"]))

    def test_source_citation_and_hyphen_characters_never_reconstructed(self):
        page = self.doc.new_page()
        page.insert_text((60, 90), "Original [7] x = y + z; hyphen-", fontsize=11)
        page.insert_text((60, 105), "ated source; no missing reference is supplied.", fontsize=11)
        for markdown in (True, False):
            report, source, _ = self.export(markdown)
            self.assertNotIn("hyphenated", source)
            self.assertIn(r"\[7\]" if markdown else "[7]", source)
            self.assertIn(r"hyphen\-" if markdown else "hyphen-", source)
            self.assertIn("ated source; no missing reference is supplied", source)
            self.assertIn("not semantically reconstructed", " ".join(report["warnings"]))

    def test_exports_do_not_mutate_source(self):
        page = self.doc.new_page()
        body(page)
        page.set_rotation(90)
        text, metadata, rotation = page.get_text(), dict(self.doc.metadata), page.rotation
        self.export()
        self.export(False)
        self.assertEqual(text, self.doc[0].get_text())
        self.assertEqual(metadata, self.doc.metadata)
        self.assertEqual(rotation, self.doc[0].rotation)

    def test_strict_options_reject_wrong_types_and_unknown_keys(self):
        self.doc.new_page()
        for markdown in (True, False):
            for options in (
                [],
                "",
                {"skip_repeated_margins": 1},
                {"skip_repeated_margins": "true"},
                {"unknown": True},
            ):
                with (
                    self.subTest(markdown=markdown, options=options),
                    self.assertRaises(ValueError),
                ):
                    self.export(markdown, options)
        with self.assertRaises(ValueError):
            self.export(options={"include_images": "false"})
        with self.assertRaises(ValueError):
            self.export(False, {"include_images": True})


if __name__ == "__main__":
    unittest.main()
