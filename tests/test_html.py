"""Real optional-engine regressions for resource denial and generated images."""

import base64
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from pdf_multitool.core import Job


@unittest.skipUnless(importlib.util.find_spec("weasyprint"), "HTML extra is not installed")
class HTMLIntegrationTests(unittest.TestCase):
    def setUp(self):
        from pdf_multitool.html_resources import DenyAllFetcher, GeneratedImageFetcher

        self.deny = DenyAllFetcher
        self.images = GeneratedImageFetcher
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def job(self, operation, source):
        job = Job(
            operation,
            [{"id": "1", "name": source.name, "path": str(source)}],
            {},
            self.root / "out",
        )
        job.run()
        self.assertEqual(job.state, "completed", job.errors)
        return job

    def test_denial_never_reaches_default_transport(self):
        fetcher = self.deny()
        urls = (
            "https://example.invalid/image.png",
            "http://127.0.0.1/private",
            "file:///etc/passwd",
            "data:image/png;base64,AAAA",
            "ftp://example.invalid/image.png",
        )
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("transport")):
            for url in urls:
                with self.subTest(url=url), self.assertRaisesRegex(ValueError, "disabled"):
                    fetcher(url)
        self.assertEqual(fetcher.blocked, {"https", "http", "file", "data", "ftp"})

    def test_generated_images_use_response_api_and_remain_bounded(self):
        fetcher = self.images()
        raw = b"synthetic bytes; image decoding is separately validated by the engine"
        url = "data:image/png;base64," + base64.b64encode(raw).decode()
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("transport")):
            response = fetcher(url)
            try:
                self.assertEqual(response.read(), raw)
                self.assertEqual(response.content_type, "image/png")
            finally:
                response.close()
            for invalid in (
                "https://example.invalid/image.png",
                "file:///etc/passwd",
                "data:text/html;base64,AAAA",
                "data:image/svg+xml;base64,AAAA",
                "data:image/png;base64,A",
                "data:image/png;base64,%%%%",
            ):
                with self.subTest(url=invalid), self.assertRaises(ValueError):
                    fetcher(invalid)
            # A small test cap exercises the boundary without allocating 32 MiB.
            with (
                patch.object(fetcher, "MAX_ENCODED_BYTES", 4),
                self.assertRaisesRegex(ValueError, "size limit"),
            ):
                fetcher("data:image/png;base64," + "A" * 8)

    def test_html_text_survives_blocked_resources(self):
        source = self.root / "input.html"
        source.write_text(
            '<style>@import url("https://example.invalid/style.css");</style>'
            '<h1>Readable offline text</h1><img src="file:///nonexistent-image.png">'
            '<img src="data:image/png;base64,AAAA">'
        )
        original = source.read_bytes()
        job = self.job("html", source)
        self.assertTrue(any("resource loading was blocked" in w for w in job.warnings))
        with pymupdf.open(job.results[0]["path"]) as document:
            self.assertIn("Readable offline text", document[0].get_text())
            self.assertEqual(document[0].get_images(), [])
        self.assertEqual(source.read_bytes(), original)

    def test_renderer_denies_existing_local_css_font_and_attachments(self):
        from dataclasses import asdict

        from pdf_multitool.core import Limits
        from pdf_multitool.worker import process

        sentinel = self.root / "sentinel.txt"
        sentinel.write_text("PRIVATE_ATTACHMENT_SENTINEL")
        css = self.root / "sentinel.css"
        css.write_text('h1::after { content: "PRIVATE_CSS_SENTINEL"; }')
        # A valid font ensures failure cannot be explained by malformed font bytes.
        font = self.root / "sentinel.ttf"
        font.write_bytes(pymupdf.Font("helv").buffer)
        source = self.root / "input.html"
        source.write_text(
            f'<link rel="stylesheet" href="{css.as_uri()}">'
            f'<style>@import url("{css.as_uri()}");'
            f'@font-face {{font-family: Private; src: url("{font.as_uri()}");}}'
            "h1 {font-family: Private}</style>"
            f'<link rel="attachment" href="{sentinel.as_uri()}" title="private">'
            f'<h1>Public heading</h1><a rel="attachment" href="{sentinel.as_uri()}">'
            "Attachment</a>"
        )
        original = source.read_bytes()
        out = self.root / "render"
        out.mkdir()
        from pdf_multitool.html_resources import DenyAllFetcher

        attempted = []
        deny = DenyAllFetcher.fetch

        def record(fetcher, url, headers=None):
            attempted.append(url)
            return deny(fetcher, url, headers)

        # Exercise the complete production renderer, including write_pdf attachments.
        with (
            patch.object(DenyAllFetcher, "fetch", record),
            patch(
                "urllib.request.OpenerDirector.open", side_effect=AssertionError("transport")
            ) as transport,
        ):
            result = process(
                {
                    "operation": "html",
                    "inputs": [str(source)],
                    "names": [source.name],
                    "options": {},
                    "workdir": str(out),
                    "limits": asdict(Limits()),
                }
            )
        transport.assert_not_called()
        self.assertTrue({css.as_uri(), font.as_uri(), sentinel.as_uri()} <= set(attempted))
        self.assertTrue(result["warnings"])
        with pymupdf.open(out / "document.pdf") as pdf:
            text = "".join(page.get_text() for page in pdf)
            self.assertIn("Public heading", text)
            self.assertNotIn("PRIVATE_", text)
            self.assertEqual(pdf.embfile_count(), 0)
            self.assertFalse(any(list(page.annots() or []) for page in pdf))
        self.assertEqual(source.read_bytes(), original)

    def test_repagination_renders_generated_image_and_preserves_original(self):
        source = self.root / "source.pdf"
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 12, 12))
        pixmap.clear_with(128)
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "Repagination image fixture")
            page.insert_image(pymupdf.Rect(72, 100, 172, 200), stream=pixmap.tobytes("png"))
            document.save(source)
        original = source.read_bytes()
        job = self.job("repaginate", source)
        result = next(item for item in job.results if item["name"].endswith(".pdf"))
        with pymupdf.open(result["path"]) as document:
            self.assertTrue(
                any("Repagination image fixture" in page.get_text() for page in document)
            )
            self.assertTrue(any(page.get_images() for page in document))
        self.assertEqual(source.read_bytes(), original)
