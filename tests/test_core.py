import hashlib
import shutil
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pymupdf

from pdf_multitool.core import (
    Job,
    JobError,
    Limits,
    directory_size,
    parse_pages,
    run_worker,
    safe_name,
)
from pdf_multitool.models import validate_model_manifest
from tests.fixtures import document, scanned


class SelectionTests(unittest.TestCase):
    def test_order_deduplication(self):
        self.assertEqual(parse_pages("3,1-2,3", 3), [2, 0, 1])

    def test_bad_ranges(self):
        for value in ["0", "4", "3-1", "1;2", "-1", "1,,2", "1-a"]:
            with self.subTest(value=value), self.assertRaises(JobError):
                parse_pages(value, 3)

    def test_safe_names(self):
        self.assertEqual(safe_name("../../hello\n.pdf"), "hello.pdf")
        self.assertEqual(safe_name("C:\\secret\\file.pdf"), "file.pdf")

    def test_long_names_preserve_type_suffix_and_utf8_boundary(self):
        for name in [
            "a" * 210 + ".pdf",
            "é" * 210 + ".html",
            "a" * 210 + ".pdf.gz",
            "a" * 210 + ".tar.gz",
        ]:
            result = safe_name(name)
            self.assertLessEqual(len(result.encode("utf-8")), 180)
            self.assertEqual(result.rsplit(".", 1)[-1], name.rsplit(".", 1)[-1])
        self.assertTrue(safe_name("a" * 210 + ".pdf.gz").endswith(".pdf.gz"))
        self.assertTrue(safe_name("a" * 210 + ".jpeg.gz").endswith(".jpeg.gz"))

    def test_models_fail_closed(self):
        with self.assertRaises(JobError):
            validate_model_manifest(None)

    def test_quota_scan_ignores_disappeared_cache_entry_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "stable").write_bytes(b"12345")
            (root / "vanishing.tmp").write_bytes(b"cache")
            original = Path.stat

            def vanished(path, *args, **kwargs):
                if path.name == "vanishing.tmp":
                    raise FileNotFoundError("atomically removed")
                return original(path, *args, **kwargs)

            with patch.object(Path, "stat", vanished):
                self.assertEqual(directory_size(root), 5)

            def denied(path, *args, **kwargs):
                if path.name == "vanishing.tmp":
                    raise PermissionError("not a disappearing-entry race")
                return original(path, *args, **kwargs)

            with patch.object(Path, "stat", denied), self.assertRaises(PermissionError):
                directory_size(root)
            with self.assertRaisesRegex(JobError, "file-count"):
                directory_size(root, max_entries=1)


class RealEngineTests(unittest.TestCase):
    """These exercise actual preinstalled engines, not mocks or verified releases."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.a = document(self.root / '- odd "quotes"; $(touch NOT_CREATED).pdf')
        self.b = document(self.root / "second.pdf", 2)
        self.before = hashlib.sha256(self.a.read_bytes()).hexdigest()

    def tearDown(self):
        self.assertEqual(hashlib.sha256(self.a.read_bytes()).hexdigest(), self.before)
        self.assertFalse((self.root / "NOT_CREATED").exists())
        self.temp.cleanup()

    def job(self, operation, inputs=None, options=None):
        paths = inputs or [self.a]
        files = [{"id": str(i), "name": p.name, "path": str(p)} for i, p in enumerate(paths)]
        job = Job(operation, files, options or {}, self.root / "out")
        job.run()
        return job

    def test_merge_real_integrity(self):
        job = self.job("merge", [self.a, self.b])
        self.assertEqual(job.state, "completed", job.errors)
        with pymupdf.open(job.results[0]["path"]) as doc:
            self.assertEqual(len(doc), 5)
            self.assertIn("Page 3", doc[2].get_text())
            self.assertIn("Page 1", doc[3].get_text())

    def test_split_page_groups(self):
        job = self.job("split", options={"pages": "3;1-2"})
        self.assertEqual(job.state, "completed", job.errors)
        self.assertEqual(len(job.results), 2)
        with pymupdf.open(job.results[0]["path"]) as doc:
            self.assertEqual(len(doc), 1)
            self.assertIn("Page 3", doc[0].get_text())

    def test_rotate_selected_page(self):
        job = self.job("rotate", options={"pages": "2", "rotation": 90})
        self.assertEqual(job.state, "completed", job.errors)
        with pymupdf.open(job.results[0]["path"]) as doc:
            self.assertEqual([page.rotation for page in doc], [0, 90, 0])

    @unittest.skipUnless(shutil.which("pdftotext"), "Poppler not installed")
    def test_text_real_selected_order(self):
        job = self.job("text", options={"pages": "3,1"})
        self.assertEqual(job.state, "completed", job.errors)
        text = Path(job.results[0]["path"]).read_text()
        self.assertLess(text.index("Page 3"), text.index("Page 1"))
        self.assertNotIn("Page 2", text)
        self.assertIn("\f", text)

    @unittest.skipUnless(shutil.which("pdftotext"), "Poppler not installed")
    def test_scanned_text_is_not_fake_success(self):
        path = scanned(self.root / "scan.pdf")
        job = self.job("text", [path])
        self.assertEqual(job.state, "completed", job.errors)
        self.assertTrue(any("may need OCR" in w for w in job.warnings))

    @unittest.skipUnless(shutil.which("pdftoppm"), "Poppler not installed")
    def test_render_real_png(self):
        job = self.job("render", options={"pages": "2", "dpi": 72})
        self.assertEqual(job.state, "completed", job.errors)
        path = Path(job.results[0]["path"])
        self.assertTrue(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        pix = pymupdf.Pixmap(path)
        self.assertGreater(pix.width, 100)
        self.assertIn("0002", job.results[0]["name"])

    @unittest.skipUnless(shutil.which("pdftoppm"), "Poppler not installed")
    def test_render_dpi_never_unconditionally_upscales(self):
        tiny = self.root / "one-inch.pdf"
        with pymupdf.open() as doc:
            doc.new_page(width=72, height=72)
            doc.save(tiny)
        for dpi in [36, 72]:
            job = self.job("render", [tiny], {"dpi": dpi})
            self.assertEqual(job.state, "completed", job.errors)
            pix = pymupdf.Pixmap(job.results[0]["path"])
            self.assertEqual((pix.width, pix.height), (dpi, dpi))

    @unittest.skipUnless(shutil.which("gs"), "Ghostscript not installed")
    def test_compression_explicit_acknowledgement(self):
        blocked = self.job("compress")
        self.assertEqual(blocked.state, "failed")
        self.assertIn("acknowledge", blocked.errors[0]["error"])
        job = self.job("compress", options={"allow_lossy": True})
        self.assertEqual(job.state, "completed", job.errors)
        with pymupdf.open(job.results[0]["path"]) as doc:
            self.assertEqual(len(doc), 3)
            self.assertIn("visible test content", doc[0].get_text())

    def test_malformed_partial_batch(self):
        broken = self.root / "broken.pdf"
        broken.write_bytes(b"%PDF-1.7 garbage not a real document")
        job = self.job("rotate", [self.a, broken])
        self.assertEqual(job.state, "partial")
        self.assertEqual(len(job.results), 1)
        self.assertEqual(len(job.errors), 1)

    def test_bad_merge_is_atomic(self):
        broken = self.root / "broken.pdf"
        broken.write_bytes(b"invalid")
        job = self.job("merge", [self.a, broken])
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.results, [])

    def test_cancel_before_start(self):
        job = Job(
            "rotate", [{"id": "1", "name": self.a.name, "path": str(self.a)}], {}, self.root / "out"
        )
        job.cancel_event.set()
        job.run()
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(job.results, [])

    def test_cancel_running_engine(self):
        many = document(self.root / "many.pdf", 100)
        job = Job(
            "render",
            [{"id": "1", "name": many.name, "path": str(many)}],
            {"dpi": 300},
            self.root / "out",
        )
        thread = threading.Thread(target=job.run)
        thread.start()
        time.sleep(0.15)
        job.cancel_event.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "Engine process failed to terminate")
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(job.results, [])

    def test_supervisor_timeout(self):
        many = document(self.root / "many.pdf", 100)
        limits = replace(Limits(), timeout_seconds=0)
        with self.assertRaisesRegex(JobError, "exceeded"):
            run_worker(
                {
                    "operation": "render",
                    "inputs": [str(many)],
                    "names": [many.name],
                    "options": {"dpi": 300},
                },
                self.root / "worker",
                threading.Event(),
                limits,
            )


if __name__ == "__main__":
    unittest.main()
