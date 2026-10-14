"""Synthetic-only security and round-trip tests for bounded archive intake."""
from dataclasses import replace
import gzip
import io
import os
from pathlib import Path
import stat
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from pdf_multitool.archives import ArchiveError, ArchiveLimits, archive_output, extract_archive


PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
HTML = b"<!DOCTYPE html><html><body><h1>Synthetic</h1></body></html>"


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / "out"
        self.out.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def zip(self, entries, compression=zipfile.ZIP_STORED):
        source = self.root / "input.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(source, "w", compression=compression) as archive:
                for name, data in entries:
                    archive.writestr(name, data)
        return source

    def tar(self, entries, mode="w", format=tarfile.PAX_FORMAT):
        source = self.root / ("input.tar.gz" if mode == "w:gz" else "input.tar")
        with tarfile.open(source, mode, format=format) as archive:
            for entry in entries:
                if isinstance(entry[0], tarfile.TarInfo):
                    info, data = entry
                else:
                    info, data = tarfile.TarInfo(entry[0]), entry[1]
                    info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return source

    def extract(self, source, **kwargs):
        return extract_archive(source, self.out, source.name, **kwargs)

    def fails_clean(self, source, match=None, **kwargs):
        with self.assertRaisesRegex(ArchiveError, match or "."):
            self.extract(source, **kwargs)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_mixed_zip_flat_manifest_and_private_permissions(self):
        source = self.zip([("folder/report.pdf", PDF), ("page.HTML", HTML), ("readme.txt", b"text"), ("nested.zip", b"PK\x05\x06" + b"\0" * 18)])
        result = self.extract(source)
        self.assertEqual([item["name"] for item in result["files"]], ["report.pdf", "page.HTML"])
        self.assertEqual(result["files"][0]["member_path"], "folder/report.pdf")
        self.assertEqual(len(result["warnings"]), 2)
        self.assertIn("nested archive", result["warnings"][1])
        self.assertEqual(result["statistics"]["entries"], 4)
        for item in result["files"]:
            path = Path(item["path"])
            self.assertEqual(path.parent, self.out)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_size, item["size"])
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)

    def test_deflated_zip(self):
        result = self.extract(self.zip([("report.pdf", PDF)], zipfile.ZIP_DEFLATED))
        self.assertEqual(Path(result["files"][0]["path"]).read_bytes(), PDF)

    def test_zip_paths_atomic(self):
        for name in ["../escape.pdf", "/escape.pdf", "C:/escape.pdf", "C:escape.pdf", "\\\\server\\share.pdf", "folder\\escape.pdf", "a/../../escape.pdf", "bad\n.pdf", "trailing./a.pdf", "NUL.pdf"]:
            with self.subTest(name=name):
                self.fails_clean(self.zip([("safe.pdf", PDF), (name, PDF)]))
        self.assertFalse((self.root / "escape.pdf").exists())

    def test_zip_nul_filename_rejected(self):
        source = self.zip([("badX.pdf", PDF)])
        source.write_bytes(source.read_bytes().replace(b"badX.pdf", b"bad\0.pdf"))
        self.fails_clean(source, "control")

    def test_duplicate_normalization_and_case_collisions(self):
        for names in [("a.pdf", "a.pdf"), ("a.pdf", "./a.pdf"), ("a.pdf", "A.pdf"), ("caf\u00e9.pdf", "cafe\u0301.pdf"), ("Dir/a.pdf", "dir/b.pdf"), ("a.pdf", "a.pdf/b.pdf")]:
            with self.subTest(names=names):
                self.fails_clean(self.zip([(name, PDF) for name in names]), "[Cc]ollision|Duplicate")

    def test_zip_special_types_rejected(self):
        for kind in [stat.S_IFLNK, stat.S_IFCHR, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFBLK]:
            with self.subTest(kind=kind):
                info = zipfile.ZipInfo("link.pdf")
                info.create_system = 3
                info.external_attr = (kind | 0o777) << 16
                self.fails_clean(self.zip([("safe.pdf", PDF), (info, b"/target")]), "special")

    def test_zip_encryption_rejected(self):
        source = self.zip([("safe.pdf", PDF)])
        raw = bytearray(source.read_bytes())
        struct.pack_into("<H", raw, 6, 1)
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<H", raw, central + 8, 1)
        source.write_bytes(raw)
        self.fails_clean(source, "Encrypted")

    def test_zip_crc_failure_atomic(self):
        source = self.zip([("safe.pdf", PDF), ("other.pdf", PDF)])
        raw = bytearray(source.read_bytes())
        at = raw.rfind(PDF)
        raw[at + 15] ^= 1
        source.write_bytes(raw)
        self.fails_clean(source, "CRC")

    def test_zip_skipped_member_crc_is_still_verified(self):
        source = self.zip([("safe.pdf", PDF), ("ignored.txt", b"synthetic text")])
        source.write_bytes(source.read_bytes().replace(b"synthetic text", b"SYnthetic text"))
        self.fails_clean(source, "CRC")

    def test_zip_actual_bytes_not_forged_size(self):
        source = self.zip([("bomb.pdf", PDF + b"X" * 10000)], zipfile.ZIP_DEFLATED)
        raw = bytearray(source.read_bytes())
        struct.pack_into("<I", raw, 22, len(PDF))
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<I", raw, central + 24, len(PDF))
        source.write_bytes(raw)
        self.fails_clean(source, "member expanded-byte", limits=replace(ArchiveLimits(), max_member_bytes=1024, max_compression_ratio=10000))

    def test_zip_ratio_quota(self):
        self.fails_clean(self.zip([("bomb.pdf", PDF + b"X" * 100000)], zipfile.ZIP_DEFLATED), "ratio")

    def test_zip_declared_compressed_size_and_stream_truncation(self):
        source = self.zip([("safe.pdf", PDF)], zipfile.ZIP_DEFLATED)
        raw = bytearray(source.read_bytes())
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<I", raw, central + 20, 2)
        source.write_bytes(raw)
        self.fails_clean(source, "Truncated")

    def test_zip_overlapping_member_data(self):
        source = self.zip([("same.pdf", PDF), ("other.pdf", PDF)])
        raw = bytearray(source.read_bytes())
        second = raw.index(b"PK\x01\x02", raw.index(b"PK\x01\x02") + 4)
        struct.pack_into("<I", raw, second + 42, 0)
        source.write_bytes(raw)
        self.fails_clean(source, "disagree|Overlapping")

    def test_entry_quota_preflight_cannot_trust_eocd_count(self):
        source = self.zip([(f"{i}.txt", b"x") for i in range(4)])
        raw = bytearray(source.read_bytes())
        end = raw.index(b"PK\x05\x06")
        struct.pack_into("<HH", raw, end + 8, 1, 1)
        source.write_bytes(raw)
        self.fails_clean(source, "entry limit", limits=replace(ArchiveLimits(), max_entries=2))

    def test_each_intake_limit(self):
        entries = [("one.pdf", PDF), ("two.pdf", PDF)]
        cases = [({"max_compressed_bytes": 1}, "compressed-byte"), ({"max_expanded_bytes": len(PDF)}, "total expanded-byte"), ({"max_member_bytes": 4}, "member expanded-byte"), ({"max_files": 1}, "accepted-file"), ({"max_entries": 1}, "entry limit")]
        for fields, text in cases:
            with self.subTest(fields=fields):
                self.fails_clean(self.zip(entries), text, limits=replace(ArchiveLimits(), **fields))
        self.fails_clean(self.zip([("a/b/c.pdf", PDF)]), "path-depth", limits=replace(ArchiveLimits(), max_path_depth=2))

    def test_unsupported_members_count_toward_byte_quotas(self):
        source = self.zip([("skip.dat", b"X" * 1000)])
        self.fails_clean(source, "expanded-byte", limits=replace(ArchiveLimits(), max_expanded_bytes=100))

    def test_content_sniffing_and_disguised_nested_archives(self):
        result = self.extract(self.zip([("fake.pdf", b"no document"), ("fake.html", b"binary\0stuff"), ("nested.pdf", b"PK\x03\x04"), ("real.htm", HTML)]))
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(len(result["warnings"]), 3)
        self.assertIn("nested archive", result["warnings"][2])

    def test_tar_and_targz_roundtrip(self):
        for mode in ["w", "w:gz"]:
            with self.subTest(mode=mode):
                source = self.tar([("folder/report.pdf", PDF), ("page.html", HTML)], mode)
                result = self.extract(source)
                self.assertEqual(len(result["files"]), 2)
                self.assertEqual(Path(result["files"][0]["path"]).read_bytes(), PDF)
                for child in self.out.iterdir():
                    child.unlink()

    def test_tar_unicode_pax_and_gnu_longname(self):
        for format, name in [(tarfile.PAX_FORMAT, "caf\u00e9.pdf"), (tarfile.GNU_FORMAT, "a" * 120 + ".pdf")]:
            with self.subTest(format=format):
                result = self.extract(self.tar([(name, PDF)], format=format))
                self.assertEqual(result["files"][0]["name"], name)
                self.assertEqual(result["statistics"]["entries"], 2)
                for child in self.out.iterdir():
                    child.unlink()

    def test_tar_special_files_rejected(self):
        for kind in [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.GNUTYPE_SPARSE]:
            with self.subTest(kind=kind):
                info = tarfile.TarInfo("link.pdf")
                info.type = kind
                info.linkname = "/outside"
                self.fails_clean(self.tar([("safe.pdf", PDF), (info, b"")]), "rejected")

    def test_tar_malicious_paths_and_duplicate(self):
        for name in ["../bad.pdf", "/absolute.pdf", "C:/bad.pdf", "safe.pdf", "SAFE.pdf"]:
            with self.subTest(name=name):
                self.fails_clean(self.tar([("safe.pdf", PDF), (name, PDF)]))

    def test_pax_path_override_and_sparse_rejected(self):
        for metadata in [{"path": "../evil.pdf"}, {"GNU.sparse.size": "20"}, {"GNU.sparse.map": "0,20"}, {"SCHILY.filetype": "sparse"}, {"linkpath": "/outside"}]:
            with self.subTest(metadata=metadata):
                info = tarfile.TarInfo("safe.pdf")
                info.size = len(PDF)
                info.pax_headers = metadata
                self.fails_clean(self.tar([(info, PDF)]))

    def test_tar_metadata_counts_toward_entry_limit(self):
        source = self.tar([("caf\u00e9.pdf", PDF)])
        self.fails_clean(source, "entry limit", limits=replace(ArchiveLimits(), max_entries=1))

    def test_tar_truncated_and_trailing_data(self):
        source = self.tar([("safe.pdf", PDF)])
        original = source.read_bytes()
        for raw in [original[:520], original[:1024], original + b"hidden archive"]:
            with self.subTest(size=len(raw)):
                source.write_bytes(raw)
                self.fails_clean(source, "Truncated|end marker")

    def test_gzip_single_and_concatenated_members(self):
        source = self.root / "document.pdf.gz"
        source.write_bytes(gzip.compress(PDF[:3]) + gzip.compress(PDF[3:]))
        result = self.extract(source)
        self.assertEqual(result["statistics"]["format"], "gzip")
        self.assertEqual(result["statistics"]["entries"], 1)
        self.assertEqual(Path(result["files"][0]["path"]).read_bytes(), PDF)

    def test_gzip_bad_crc_and_truncated_and_trailing_data(self):
        source = self.root / "document.pdf.gz"
        valid = gzip.compress(PDF)
        bad = bytearray(valid)
        bad[-8] ^= 1
        for raw in [bad, valid[:-4], valid + b"hidden data"]:
            with self.subTest(size=len(raw)):
                source.write_bytes(raw)
                self.fails_clean(source)

    def test_gzip_actual_expansion_and_ratio_bounds(self):
        source = self.root / "document.pdf.gz"
        source.write_bytes(gzip.compress(PDF + b"a" * 100000))
        self.fails_clean(source, "ratio")
        self.fails_clean(source, "member expanded-byte", limits=replace(ArchiveLimits(), max_compression_ratio=10000, max_member_bytes=1000))

    def test_nested_gzip_is_not_expanded(self):
        source = self.root / "nested.zip.gz"
        source.write_bytes(gzip.compress(b"PK\x05\x06" + b"\0" * 18))
        result = self.extract(source)
        self.assertEqual(result["files"], [])
        self.assertIn("nested archive", result["warnings"][0])

    def test_cancellation_and_timeout_cleanup(self):
        source = self.zip([("safe.pdf", PDF), ("other.pdf", PDF)])
        def cancelled():
            return bool(list(self.out.iterdir()))
        self.fails_clean(source, "cancelled", cancelled=cancelled)
        with patch("pdf_multitool.archives.time.monotonic", side_effect=[0, 31]):
            self.fails_clean(source, "wall-time")

    def test_destination_nonempty_symlink_and_source_symlink(self):
        source = self.zip([("safe.pdf", PDF)])
        existing = self.out / "keep"
        existing.write_bytes(b"keep")
        with self.assertRaisesRegex(ArchiveError, "empty"):
            self.extract(source)
        self.assertEqual(existing.read_bytes(), b"keep")
        existing.unlink()
        link = self.root / "link"
        link.symlink_to(self.out, target_is_directory=True)
        with self.assertRaises(ArchiveError):
            extract_archive(source, link, "input.zip")
        source_link = self.root / "source-link.zip"
        source_link.symlink_to(source)
        self.fails_clean(source_link)

    def test_limits_invalid_values(self):
        for kw in [{"max_files": 0}, {"max_entries": True}, {"max_compression_ratio": float("inf")}, {"wall_time_seconds": float("nan")}]:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                ArchiveLimits(**kw)

    def test_output_zip_and_tar_preserve_relative_asset_paths(self):
        md = self.root / "document.md"
        md.write_bytes(b"![Image](document_assets/image-0001.png)")
        image = self.root / "image.png"
        image.write_bytes(b"synthetic image bytes")
        files = [{"path": str(md), "name": md.name, "archive_path": "document-0001/document.md"}, {"path": str(image), "name": image.name, "archive_path": "document-0001/document_assets/image-0001.png"}]
        for format in ["zip", "tar.gz"]:
            with self.subTest(format=format):
                destination = self.root / ("result." + format)
                result = archive_output(files, destination, format)
                self.assertEqual(result["size"], destination.stat().st_size)
                if format == "zip":
                    with zipfile.ZipFile(destination) as archive:
                        self.assertEqual(archive.namelist(), [f["archive_path"] for f in files])
                        self.assertEqual(archive.read(files[0]["archive_path"]), md.read_bytes())
                else:
                    with tarfile.open(destination) as archive:
                        self.assertEqual(archive.getnames(), [f["archive_path"] for f in files])
                        self.assertEqual(archive.extractfile(files[0]["archive_path"]).read(), md.read_bytes())

    def test_output_never_overwrites_and_rejects_unsafe_duplicates(self):
        source = self.root / "report.pdf"
        source.write_bytes(PDF)
        destination = self.root / "result.zip"
        destination.write_bytes(b"keep existing")
        item = {"path": str(source), "name": source.name}
        with self.assertRaises(ArchiveError):
            archive_output([item], destination)
        self.assertEqual(destination.read_bytes(), b"keep existing")
        destination.unlink()
        for files in [[item, item], [{**item, "archive_path": "../escape.pdf"}], [{**item, "name": "nested/report.pdf"}], [{**item, "archive_path": "a//report.pdf"}]]:
            with self.subTest(files=files), self.assertRaises(ArchiveError):
                archive_output(files, destination)
        self.assertFalse(destination.exists())

    def test_output_accepts_more_than_200_and_caps_at_1000(self):
        source = self.root / "report.pdf"
        source.write_bytes(PDF)
        items = [{"path": str(source), "name": f"report-{i:04}.pdf"} for i in range(1001)]
        destination = self.root / "result.zip"
        archive_output(items[:201], destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(len(archive.infolist()), 201)
        with self.assertRaisesRegex(ArchiveError, "1000"):
            archive_output(items, self.root / "too-many.zip")


    def test_html_fragment_and_utf16(self):
        result = self.extract(self.zip([("fragment.html", b'<a href="https://example.com">Synthetic</a>'), ("utf16.html", "<html>synthetic</html>".encode("utf-16"))]))
        self.assertEqual(len(result["files"]), 2)

    def test_output_failure_removes_partial_archive(self):
        source = self.root / "report.pdf"
        source.write_bytes(PDF)
        destination = self.root / "result.zip"
        with patch("pdf_multitool.archives._CountedSource.read", side_effect=[PDF, OSError("synthetic read failure")]):
            with self.assertRaisesRegex(ArchiveError, "synthetic read failure"):
                archive_output([{"path": str(source), "name": source.name}], destination)
        self.assertFalse(destination.exists())

    def test_output_byte_quotas_and_source_symlink(self):
        source = self.root / "report.pdf"
        source.write_bytes(PDF)
        destination = self.root / "result.zip"
        item = {"path": str(source), "name": source.name}
        with patch("pdf_multitool.archives.ArchiveLimits", return_value=replace(ArchiveLimits(), max_expanded_bytes=10)):
            with self.assertRaisesRegex(ArchiveError, "input-byte"):
                archive_output([item], destination)
        self.assertFalse(destination.exists())
        with patch("pdf_multitool.archives.ArchiveLimits", return_value=replace(ArchiveLimits(), max_expanded_bytes=100)):
            with self.assertRaisesRegex(ArchiveError, "Output archive byte"):
                archive_output([item], destination)
        self.assertFalse(destination.exists())
        link = self.root / "link.pdf"
        link.symlink_to(source)
        with self.assertRaises(ArchiveError):
            archive_output([{**item, "path": str(link)}], destination)
        self.assertFalse(destination.exists())

    def test_gzip_many_empty_members_obeys_cancellation(self):
        source = self.root / "document.pdf.gz"
        source.write_bytes(gzip.compress(b"") * 100 + gzip.compress(PDF))
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            return calls > 30
        self.fails_clean(source, "cancelled", cancelled=cancelled)

    def test_tar_skips_nested_and_unsupported_but_validates_all(self):
        result = self.extract(self.tar([("report.pdf", PDF), ("nested.zip", b"PK\x05\x06" + b"\0" * 18), ("unsupported.txt", b"synthetic")]))
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(len(result["warnings"]), 2)
        self.assertIn("nested", result["warnings"][0])


    def test_output_publication_is_atomic(self):
        source = self.root / "report.pdf"
        source.write_bytes(PDF)
        destination = self.root / "result.zip"
        chunks = iter([PDF, b""])
        def read(_size):
            self.assertFalse(destination.exists())
            return next(chunks)
        with patch("pdf_multitool.archives._CountedSource.read", side_effect=read):
            archive_output([{"path": str(source), "name": source.name}], destination)
        self.assertTrue(destination.exists())
        self.assertEqual(list(self.root.glob(".archive-*.part")), [])


if __name__ == "__main__":
    unittest.main()
