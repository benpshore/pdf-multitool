#!/usr/bin/env python3
"""Offline safety tests; fixtures are generated locally, never downloaded."""

import hashlib
import io
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import verify_pypi_artifact as verifier


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.content = b"a local non-executable test fixture"
        self.sha = hashlib.sha256(self.content).hexdigest()
        self.name = "sample-1.0-py3-none-any.whl"
        self.url = "https://files.pythonhosted.org/packages/example/" + self.name
        self.release = {
            "info": {"name": "sample", "version": "1.0"},
            "urls": [
                {
                    "filename": self.name,
                    "url": self.url,
                    "size": len(self.content),
                    "digests": {"sha256": self.sha},
                    "yanked": False,
                }
            ],
        }
        self.simple = {
            "name": "sample",
            "files": [
                {
                    "filename": self.name,
                    "url": self.url,
                    "hashes": {"sha256": self.sha},
                    "yanked": False,
                }
            ],
        }

    def compare(self):
        return verifier.compare_metadata("sample", "1.0", self.name, self.release, self.simple)

    def test_matching_official_disclosures(self):
        record = self.compare()
        self.assertEqual(record["sha256"], self.sha)
        self.assertFalse(record["artifact_downloaded"])

    def test_missing_hash_fails(self):
        self.simple["files"][0]["hashes"] = {}
        with self.assertRaises(verifier.VerificationError):
            self.compare()

    def test_mismatched_hash_fails(self):
        self.simple["files"][0]["hashes"]["sha256"] = "f" * 64
        with self.assertRaises(verifier.VerificationError):
            self.compare()

    def test_project_mismatch_fails(self):
        self.release["info"]["name"] = "different"
        with self.assertRaises(verifier.VerificationError):
            self.compare()

    def test_version_mismatch_fails(self):
        self.release["info"]["version"] = "2.0"
        with self.assertRaises(verifier.VerificationError):
            self.compare()

    def test_mirror_fails(self):
        self.simple["files"][0]["url"] = self.url.replace(
            "files.pythonhosted.org", "mirror.example.com"
        )
        with self.assertRaises(verifier.VerificationError):
            self.compare()

    def test_insecure_url_fails(self):
        self.release["urls"][0]["url"] = self.url.replace("https:", "http:")
        with self.assertRaises(verifier.VerificationError):
            self.compare()

    def test_yanked_fails(self):
        self.simple["files"][0]["yanked"] = "bad release"
        with self.assertRaises(verifier.VerificationError):
            self.compare()

    def test_duplicate_filename_fails(self):
        self.simple["files"].append(deepcopy(self.simple["files"][0]))
        with self.assertRaises(verifier.VerificationError):
            self.compare()

    def test_source_build_fails(self):
        with self.assertRaises(verifier.VerificationError):
            verifier.compare_metadata(
                "sample", "1.0", "sample-1.0.tar.gz", self.release, self.simple
            )

    def test_oversize_does_not_open_network(self):
        record = self.compare()
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(verifier.urllib.request, "build_opener") as opener,
        ):
            with self.assertRaises(verifier.VerificationError):
                verifier.download(record, Path(temp), 1)
            opener.assert_not_called()

    def test_byte_verification(self):
        record = self.compare()
        response = io.BytesIO(self.content)
        response.geturl = lambda: self.url
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(verifier.urllib.request, "build_opener") as opener,
        ):
            opener.return_value.open.return_value = response
            target = verifier.download(record, Path(temp), 1024)
            self.assertEqual(target.read_bytes(), self.content)
            self.assertTrue(record["artifact_downloaded"])

    def test_wrong_bytes_removed(self):
        record = self.compare()
        response = io.BytesIO(b"x" * len(self.content))
        response.geturl = lambda: self.url
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(verifier.urllib.request, "build_opener") as opener,
        ):
            opener.return_value.open.return_value = response
            with self.assertRaises(verifier.VerificationError):
                verifier.download(record, Path(temp), 1024)
            self.assertEqual(list(Path(temp).iterdir()), [])
            self.assertFalse(record["artifact_downloaded"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
