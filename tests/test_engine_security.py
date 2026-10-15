"""Regression for the dependency advisory; the app does not call embed-extract."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pymupdf


class EmbeddedExtractionSafetyTests(unittest.TestCase):
    def extract(self, root, filename, existing=False):
        source = root / "attachment.pdf"
        with pymupdf.open() as document:
            document.new_page()
            document.embfile_add("fixture", b"synthetic attachment", filename=filename)
            document.save(source)
        destination = root / "extract"
        destination.mkdir()
        if existing:
            (destination / "existing.txt").write_bytes(b"preserved sentinel")
        return subprocess.run(  # noqa: S603 -- fixed interpreter/module argv; synthetic PDF
            [sys.executable, "-m", "pymupdf", "embed-extract", str(source), "-name", "fixture"],
            cwd=destination,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def test_default_extraction_rejects_escape_and_existing_files(self):
        for kind in ("parent", "absolute", "existing"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / "outside.txt"
                filename = "../outside.txt" if kind == "parent" else str(target)
                if kind == "existing":
                    target = root / "extract" / "existing.txt"
                    filename = "existing.txt"
                result = self.extract(root, filename, existing=kind == "existing")
                if kind == "existing":
                    self.assertEqual(target.read_bytes(), b"preserved sentinel")
                else:
                    self.assertFalse(target.exists(), "Attachment escaped the extraction directory")
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_default_extraction_allows_new_local_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.extract(root, "safe.txt")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "extract" / "safe.txt").read_bytes(), b"synthetic attachment")
