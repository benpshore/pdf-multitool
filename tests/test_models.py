"""Synthetic manifest protocol tests; these fixtures do not authenticate model publishers."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pdf_multitool.core import Job, JobError, engines
from pdf_multitool.models import validate_model_manifest
from tests.fixtures import document


class ModelManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.file = self.artifacts / "synthetic.bin"
        self.file.write_bytes(b"synthetic fixture, not an executable model")
        digest = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.data = {
            "artifacts_path": "artifacts",
            "files": [
                {
                    "path": self.file.name,
                    "sha256": digest,
                    "upstream_sha256": digest,
                    "source_url": "https://example.invalid/synthetic.bin",
                    "checksum_url": "https://example.invalid/checksums",
                }
            ],
        }
        self.manifest = self.root / "manifest.json"

    def write(self, data):
        self.manifest.write_text(json.dumps(data))
        return str(self.manifest)

    def test_valid_record_and_hash_mutation(self):
        self.assertEqual(validate_model_manifest(self.write(self.data)), self.artifacts)
        self.file.write_bytes(b"changed bytes")
        with self.assertRaisesRegex(JobError, "integrity"):
            validate_model_manifest(str(self.manifest))

    def test_malformed_shapes_disable_only_docling(self):
        cases = [
            None,
            [],
            {},
            {"artifacts_path": 7},
            {"artifacts_path": "artifacts", "files": "bad"},
        ]
        for bad in [None, "bad", [], {}, {"path": []}, {"path": "synthetic.bin", "sha256": 42}]:
            cases.append({"artifacts_path": "artifacts", "files": [bad]})
        for field in ("source_url", "checksum_url"):
            data = copy.deepcopy(self.data)
            data["files"][0][field] = 42
            cases.append(data)
        for data in cases:
            with (
                self.subTest(data=data),
                patch.dict("os.environ", {"PDF_MULTITOOL_MODELS": self.write(data)}),
                patch("pdf_multitool.core._package", side_effect=lambda _: {"available": True}),
            ):
                status = engines()
                self.assertFalse(status["docling"]["available"])
                self.assertTrue(status["docling"]["reason"])
                self.assertTrue(status["pymupdf"]["available"])

    def test_unlisted_file_and_escape_still_rejected(self):
        self.write(self.data)
        extra = self.artifacts / "unlisted.bin"
        extra.write_bytes(b"unlisted")
        with self.assertRaisesRegex(JobError, "Unlisted"):
            validate_model_manifest(str(self.manifest))
        extra.unlink()
        outside = self.root / "outside.bin"
        outside.write_bytes(self.file.read_bytes())
        self.data["files"][0]["path"] = "../outside.bin"
        with self.assertRaisesRegex(JobError, "escapes"):
            validate_model_manifest(self.write(self.data))

    def test_missing_mismatched_evidence_and_nonhex_digest_rejected(self):
        for field, value in [
            ("upstream_sha256", "0" * 64),
            ("source_url", "http://example.invalid"),
            ("checksum_url", ""),
            ("sha256", "z" * 64),
        ]:
            data = copy.deepcopy(self.data)
            data["files"][0][field] = value
            with self.subTest(field=field), self.assertRaises(JobError):
                validate_model_manifest(self.write(data))

    def test_duplicate_json_keys_and_manifest_size_rejected(self):
        self.manifest.write_text('{"files": [], "files": []}')
        with self.assertRaisesRegex(JobError, "Duplicate"):
            validate_model_manifest(str(self.manifest))
        self.manifest.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaisesRegex(JobError, "too large"):
            validate_model_manifest(str(self.manifest))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO regression requires POSIX")
    def test_fifo_manifest_cannot_block_workbench_startup(self):
        fifo = self.root / "manifest.fifo"
        os.mkfifo(fifo)
        alias = self.root / "manifest-link"
        alias.symlink_to(fifo)
        code = """
import os, sys
from pathlib import Path
from unittest.mock import patch
from pdf_multitool.server import Workbench
os.environ['PDF_MULTITOOL_MODELS'] = sys.argv[1]
root = Path(sys.argv[2]); root.mkdir()
with patch('pdf_multitool.core._package', side_effect=lambda _: {'available': True}):
    app = Workbench(root)
try:
    assert not app.engine_status['docling']['available']
    assert 'regular local file' in app.engine_status['docling']['reason']
    assert app.engine_status['pymupdf']['available']
finally:
    app.close()
"""
        for index, path in enumerate((fifo, alias)):
            with self.subTest(path=path):
                # A regression blocks only this disposable child; timeout kills/reaps it.
                result = subprocess.run(  # noqa: S603 -- fixed code and synthetic fixture paths
                    [sys.executable, "-c", code, str(path), str(self.root / f"session-{index}")],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "posix", "Symlink setup requires POSIX")
    def test_manifest_symlink_to_regular_file_retains_existing_policy(self):
        self.write(self.data)
        alias = self.root / "manifest-link"
        alias.symlink_to(self.manifest)
        self.assertEqual(validate_model_manifest(str(alias)), self.artifacts)

    def test_real_worker_rejects_bad_manifest_and_other_engine_still_runs(self):
        source = document(self.root / "synthetic.pdf", 1)
        original = source.read_bytes()
        files = [{"id": "1", "name": source.name, "path": str(source)}]
        with patch.dict("os.environ", {"PDF_MULTITOOL_MODELS": self.write({})}):
            job = Job("docling", files, {}, self.root / "jobs")
            job.run()
            self.assertEqual(job.state, "failed")
            self.assertEqual(job.results, [])
            self.assertIn("artifacts_path", str(job.errors))
            retry = Job("rotate", files, {}, self.root / "jobs")
            retry.run()
            self.assertEqual(retry.state, "completed", retry.errors)
        self.assertEqual(source.read_bytes(), original)
