"""Real installed-codec and adversarial image tests; no network or downloads."""
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import zlib

from PIL import Image, ImageFile, ImageOps, PngImagePlugin, features
import pymupdf

from pdf_multitool.core import JobError
from pdf_multitool.images import IMAGE_SUFFIXES, ImageLimits, images_to_pdf, inspect_image
from pdf_multitool import images


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, name="fixture.png", mode="RGB", size=(32, 20), color=(220, 30, 50), **kwargs):
        path = self.root / name
        with Image.new(mode, size, color) as image:
            image.save(path, **kwargs)
        return path

    def assert_rejected(self, path, message=None):
        with self.assertRaises(JobError) as context:
            inspect_image(path)
        if message:
            self.assertIn(message.lower(), str(context.exception).lower())
        output = self.root / "rejected.pdf"
        with self.assertRaises(JobError):
            images_to_pdf([path], output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".image-pdf-*")), [])

    @staticmethod
    def png_chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    def header_png(self, width, height):
        path = self.root / "oversized.png"
        data = b"\x89PNG\r\n\x1a\n"
        data += self.png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        data += self.png_chunk(b"IDAT", zlib.compress(b"not real pixels"))
        data += self.png_chunk(b"IEND", b"")
        path.write_bytes(data)
        return path

    def render(self, path, page=0, dpi=144):
        with pymupdf.open(path) as document:
            pix = document[page].get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    def test_real_codecs_and_report(self):
        self.assertTrue(all(features.check_codec(name) for name in ("jpg", "jpg_2000", "zlib")),
                        "These tests require the existing JPEG/PNG/JPEG2000 codecs")
        inputs = [self.fixture("first.jpg"), self.fixture("second.png"),
                  self.fixture("third.jp2"), self.fixture("fourth.j2k"),
                  self.fixture("fifth.jpx", format="JPEG2000")]
        before = {path: hashlib.sha256(path.read_bytes()).digest() for path in inputs}
        for path, format_name in zip(inputs, ("JPEG", "PNG", "JPEG2000", "JPEG2000", "JPEG2000")):
            with self.subTest(format=format_name, suffix=path.suffix):
                metadata = inspect_image(path)
                self.assertEqual(metadata["format"], format_name)
                self.assertEqual((metadata["width"], metadata["height"], metadata["pixels"]), (32, 20, 640))
                self.assertEqual(metadata["frames"], 1)
                self.assertTrue(metadata["image_only"])
                self.assertFalse(metadata["ocr_performed"])
                json.dumps(metadata)
        output = self.root / "combined.pdf"
        report = images_to_pdf(inputs, output)
        self.assertEqual(report["page_count"], 5)
        self.assertEqual(report["total_pixels"], 3200)
        self.assertEqual(report["formats"], ["JPEG", "JPEG2000", "PNG"])
        self.assertTrue(all(report["verified"].values()))
        json.dumps(report)
        with pymupdf.open(output) as document:
            for page in document:
                self.assertEqual((page.rect.width, page.rect.height), (16, 10))
                self.assertEqual(page.get_text(), "")
                self.assertEqual(page.get_images()[0][2:4], (32, 20))
        for index, path in enumerate(inputs):
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before[path])
            with Image.open(path) as source, self.render(output, index) as rendered:
                expected = source.convert("RGB")
                self.assertEqual(rendered.size, expected.size)
                # No additional lossy transform between codec decode and PDF.
                self.assertEqual(rendered.tobytes(), expected.tobytes())

    def test_supported_suffixes_are_narrow(self):
        self.assertEqual(IMAGE_SUFFIXES, {".jpg", ".jpeg", ".png", ".jp2", ".jpx", ".j2k"})

    def test_case_and_unusual_names_preserved(self):
        path = self.fixture("spaces (and) 日本語 & #100.PNG", format="PNG")
        output = self.root / "résumé output (1).pdf"
        result = images_to_pdf([path], output)
        self.assertEqual(result["pages"][0]["name"], path.name)
        self.assertTrue(output.is_file())

    def test_suffix_content_mismatch(self):
        path = self.fixture("wrong.jpg", format="PNG")
        self.assert_rejected(path, "match")

    def test_unsupported_actual_format_and_fake_heic(self):
        path = self.fixture("gif.png", format="GIF")
        self.assert_rejected(path, "unsupported")
        fake = self.root / "heic.jpg"
        fake.write_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1")
        self.assert_rejected(fake, "unsupported")
        fake = self.root / "unsupported.heic"
        fake.write_bytes(b"not an image")
        self.assert_rejected(fake, "suffix")

    def test_malformed_and_empty(self):
        for content in (b"", b"not an image", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff\xd9"):
            path = self.root / "invalid.png"
            path.write_bytes(content)
            with self.subTest(content=content):
                self.assert_rejected(path)

    def test_truncation_of_every_supported_codec(self):
        for suffix in (".jpg", ".png", ".jp2", ".j2k"):
            source = self.fixture("whole" + suffix)
            raw = source.read_bytes()
            for removed in (1, 8, len(raw) // 2):
                broken = self.root / ("truncated" + suffix)
                broken.write_bytes(raw[:-removed])
                with self.subTest(suffix=suffix, removed=removed):
                    self.assert_rejected(broken, "truncat")

    def test_png_bad_crc(self):
        path = self.fixture()
        data = bytearray(path.read_bytes())
        index = data.find(b"IDAT")
        data[index + 4] ^= 1
        path.write_bytes(data)
        self.assert_rejected(path)

    def test_animated_png_rejected(self):
        path = self.root / "animated.png"
        with Image.new("RGBA", (20, 10), "red") as first, Image.new("RGBA", (20, 10), "blue") as second:
            first.save(path, save_all=True, append_images=[second], duration=50, loop=0)
        self.assert_rejected(path, "animated")

    def test_single_frame_apng_marker_rejected(self):
        path = self.fixture()
        data = path.read_bytes()
        path.write_bytes(data[:33] + self.png_chunk(b"acTL", struct.pack(">II", 1, 0)) + data[33:])
        self.assert_rejected(path, "animated")

    def test_multi_codestream_jpeg2000_rejected(self):
        path = self.fixture("multiple.jp2")
        raw = path.read_bytes()
        index = raw.index(b"jp2c") - 4
        length = struct.unpack(">I", raw[index:index + 4])[0]
        path.write_bytes(raw + raw[index:index + length])
        self.assert_rejected(path, "multiframe")

    def test_raw_j2k_concatenation_rejected(self):
        path = self.fixture("concatenated.j2k")
        raw = path.read_bytes()
        path.write_bytes(raw + raw)
        self.assert_rejected(path, "multiple images")

    def test_jpeg2000_multiple_tiles_and_layers(self):
        path = self.root / "tiles.jp2"
        with Image.new("RGB", (128, 96), "purple") as source:
            source.save(path, tile_size=(32, 32), quality_layers=[8, 4, 1])
        self.assertEqual(inspect_image(path)["format"], "JPEG2000")
        output = self.root / "tiles.pdf"
        images_to_pdf([path], output)
        with Image.open(path) as source, self.render(output) as rendered:
            self.assertEqual(rendered.tobytes(), source.convert("RGB").tobytes())

    def test_extended_jpx_container_rejected(self):
        path = self.fixture("extended.jpx", format="JPEG2000")
        raw = bytearray(path.read_bytes())
        index = raw.index(b"ftyp") + 4
        raw[index:index + 4] = b"jpx "
        path.write_bytes(raw)
        self.assert_rejected(path, "jpx")

    def test_oversized_headers_fail_before_decode(self):
        for size, phrase in (((16_001, 1), "dimension"), ((7000, 6000), "pixel"),
                             ((20_000, 20_000), "decompression bomb")):
            path = self.header_png(*size)
            with self.subTest(size=size), mock.patch.object(Image.Image, "load", side_effect=AssertionError("decoded")):
                with self.assertRaises(JobError) as context:
                    inspect_image(path)
                self.assertIn(phrase, str(context.exception).lower())
                self.assertNotIn("decoded", str(context.exception))

    def test_all_headers_and_total_preflight_before_any_decode(self):
        first, second = self.fixture("a.png"), self.fixture("b.png")
        output = self.root / "overbudget.pdf"
        with mock.patch.object(images, "_decode", side_effect=AssertionError("decoded")):
            with self.assertRaisesRegex(JobError, "Total image pixel"):
                images_to_pdf([first, second], output, limits=ImageLimits(max_total_pixels=1000))
            oversized = self.header_png(16_001, 1)
            with self.assertRaisesRegex(JobError, "dimension limit"):
                images_to_pdf([first, oversized], output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".image-pdf-*")), [])

    def test_png_preflight_has_no_implicit_pixel_decode(self):
        first, second = self.fixture("first.png"), self.fixture("second.png")
        with mock.patch.object(PngImagePlugin.PngImageFile, "load", side_effect=AssertionError("early pixel decode")):
            with self.assertRaisesRegex(JobError, "Total image pixel limit"):
                images_to_pdf([first, second], self.root / "out.pdf", limits=ImageLimits(max_total_pixels=1000))

    def test_jpeg_concatenation_rejected(self):
        path = self.fixture("concatenated.jpg")
        data = path.read_bytes()
        path.write_bytes(data + data)
        self.assert_rejected(path, "multiple images")

    def test_mpo_disguised_as_jpeg_rejected(self):
        path = self.root / "multiple.jpg"
        with Image.new("RGB", (32, 20), "red") as first, Image.new("RGB", (32, 20), "blue") as second:
            first.save(path, format="MPO", save_all=True, append_images=[second])
        self.assert_rejected(path, "multiple images")

    def test_progressive_jpeg_with_stuffed_scan_bytes(self):
        path = self.root / "progressive.jpg"
        with Image.new("RGB", (128, 64)) as source:
            source.putdata([(x * 7 % 256, x * 23 % 256, x * 41 % 256) for x in range(128 * 64)])
            source.save(path, progressive=True, quality=91)
        self.assertIn(b"\xff\x00", path.read_bytes())
        self.assertEqual(inspect_image(path)["format"], "JPEG")
        output = self.root / "progressive.pdf"
        images_to_pdf([path], output)
        with Image.open(path) as source, self.render(output) as rendered:
            self.assertEqual(rendered.tobytes(), source.convert("RGB").tobytes())

    def test_png_exif_after_idat_applied_without_early_decode(self):
        path = self.root / "exif-after.png"
        exif = Image.Exif()
        exif[274] = 6
        with Image.new("RGB", (30, 20), "red") as source:
            source.paste((0, 0, 255), (15, 0, 30, 20))
            source.save(path, exif=exif)
        raw = path.read_bytes()
        chunks, position, moved = [], 8, None
        while position < len(raw):
            length = struct.unpack(">I", raw[position:position + 4])[0]
            chunk = raw[position:position + 12 + length]
            if chunk[4:8] == b"eXIf":
                moved = chunk
            else:
                chunks.append(chunk)
            position += 12 + length
        path.write_bytes(raw[:8] + b"".join(chunks[:-1]) + moved + chunks[-1])
        metadata = inspect_image(path)
        self.assertEqual((metadata["width"], metadata["height"]), (20, 30))
        output = self.root / "after.pdf"
        images_to_pdf([path], output)
        with Image.open(path) as source, self.render(output) as rendered:
            self.assertEqual(rendered.tobytes(), ImageOps.exif_transpose(source).tobytes())

    def test_count_and_empty_list_preflight(self):
        path = self.fixture()
        with mock.patch.object(images, "_open_source", side_effect=AssertionError("opened")):
            for paths in ([], [path] * 3, (p for p in [path] * 3)):
                with self.subTest(paths=type(paths)), self.assertRaises(JobError):
                    images_to_pdf(paths, self.root / "out.pdf", limits=ImageLimits(max_files=2))

    def test_file_byte_limit(self):
        path = self.fixture()
        with mock.patch.object(images, "_preflight", side_effect=AssertionError("parsed")):
            with self.assertRaisesRegex(JobError, "input-byte"):
                inspect_image(path, limits=ImageLimits(max_file_bytes=10))

    def test_invalid_limits(self):
        for field in ImageLimits().__dict__:
            for bad in (0, -1, 1.5, True, None):
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                    ImageLimits(**{field: bad})

    def test_dpi_validation_and_embedded_dpi_ignored(self):
        path = self.fixture(dpi=(300, 300))
        for value in (True, False, None, "144", 0, 35.9, 300.1, math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaisesRegex(JobError, "DPI"):
                images_to_pdf([path], self.root / "bad.pdf", dpi=value)
        for dpi in (36, 144, 300):
            output = self.root / f"dpi-{dpi}.pdf"
            report = images_to_pdf([path], output, dpi=dpi)
            with pymupdf.open(output) as document:
                self.assertAlmostEqual(document[0].rect.width, 32 * 72 / dpi, places=4)
                self.assertAlmostEqual(document[0].rect.height, 20 * 72 / dpi, places=4)
            self.assertEqual(report["dpi"], dpi)
        output = self.root / "default.pdf"
        images_to_pdf([path], output)
        with pymupdf.open(output) as document:
            self.assertEqual(document[0].rect.width, 16)

    def test_no_destination_overwrite(self):
        path = self.fixture()
        output = self.root / "existing.pdf"
        output.write_bytes(b"Keep this file exactly")
        with self.assertRaisesRegex(JobError, "overwrite"):
            images_to_pdf([path], output)
        self.assertEqual(output.read_bytes(), b"Keep this file exactly")
        with self.assertRaisesRegex(JobError, "overwrite"):
            images_to_pdf([path], path)
        self.assertEqual(list(self.root.glob(".image-pdf-*")), [])

    def test_atomic_publication_race_never_overwrites(self):
        path = self.fixture()
        output = self.root / "racing.pdf"
        real_link = os.link
        def racing_link(source, target):
            Path(target).write_bytes(b"Other writer")
            return real_link(source, target)
        with mock.patch.object(images.os, "link", side_effect=racing_link):
            with self.assertRaises(JobError):
                images_to_pdf([path], output)
        self.assertEqual(output.read_bytes(), b"Other writer")
        self.assertEqual(list(self.root.glob(".image-pdf-*")), [])

    def test_failed_decode_removes_partial_output(self):
        good = self.fixture()
        broken = self.root / "broken.png"
        # CRC-valid framing passes verify(), but decompressed rows are incomplete.
        data = b"\x89PNG\r\n\x1a\n"
        data += self.png_chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 20, 8, 2, 0, 0, 0))
        data += self.png_chunk(b"IDAT", zlib.compress(b"incomplete pixels"))
        data += self.png_chunk(b"IEND", b"")
        broken.write_bytes(data)
        output = self.root / "failure.pdf"
        with self.assertRaises(JobError):
            images_to_pdf([good, broken], output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".image-pdf-*")), [])

    def test_failed_pdf_verification_removes_partial_output(self):
        path = self.fixture()
        with mock.patch.object(images, "_verify_pdf", side_effect=JobError("verification failure")):
            with self.assertRaisesRegex(JobError, "verification failure"):
                images_to_pdf([path], self.root / "out.pdf")
        self.assertFalse((self.root / "out.pdf").exists())
        self.assertEqual(list(self.root.glob(".image-pdf-*")), [])

    def test_source_mutation_is_detected(self):
        path = self.fixture()
        original = images._decode
        def mutate(source, *args):
            result = original(source, *args)
            with path.open("ab") as file:
                file.write(b"changed")
            return result
        with mock.patch.object(images, "_decode", side_effect=mutate):
            with self.assertRaisesRegex(JobError, "changed during processing"):
                images_to_pdf([path], self.root / "out.pdf")
        self.assertFalse((self.root / "out.pdf").exists())
        self.assertEqual(list(self.root.glob(".image-pdf-*")), [])

    def test_symlink_inputs_outputs_and_parents_rejected(self):
        path = self.fixture()
        symlink = self.root / "link.png"
        symlink.symlink_to(path)
        self.assert_rejected(symlink, "symlink")
        output = self.root / "out.pdf"
        output.symlink_to(self.root / "nonexistent.pdf")
        with self.assertRaisesRegex(JobError, "symlink"):
            images_to_pdf([path], output)
        self.assertFalse((self.root / "nonexistent.pdf").exists())
        directory = self.root / "alias"
        directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(JobError, "symlink"):
            inspect_image(directory / path.name)
        with self.assertRaisesRegex(JobError, "symlink"):
            images_to_pdf([path], directory / "another.pdf")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX special-file check")
    def test_fifo_input_fails_without_blocking(self):
        path = self.root / "fifo.png"
        os.mkfifo(path)
        self.assert_rejected(path, "regular file")

    def test_exif_orientations_render_correctly(self):
        with Image.new("RGB", (30, 20), "red") as fixture:
            fixture.paste((0, 255, 0), (0, 0, 15, 10))
            fixture.paste((0, 0, 255), (15, 10, 30, 20))
            for orientation in range(1, 9):
                path = self.root / f"rotate-{orientation}.jpg"
                exif = Image.Exif()
                exif[274] = orientation
                fixture.save(path, exif=exif, quality=95, subsampling=0)
                output = self.root / f"rotate-{orientation}.pdf"
                report = images_to_pdf([path], output)
                with Image.open(path) as source, self.render(output) as rendered:
                    expected = ImageOps.exif_transpose(source).convert("RGB")
                    self.assertEqual(rendered.size, expected.size)
                    self.assertEqual(rendered.tobytes(), expected.tobytes(), f"orientation {orientation}")
                self.assertEqual(report["pages"][0]["orientation"], orientation)

    def test_alpha_and_white_background_render_correctly(self):
        path = self.root / "alpha.png"
        with Image.new("RGBA", (30, 20), (255, 0, 0, 0)) as fixture:
            fixture.paste((255, 0, 0, 128), (10, 0, 20, 20))
            fixture.paste((0, 0, 255, 255), (20, 0, 30, 20))
            fixture.save(path)
        output = self.root / "alpha.pdf"
        report = images_to_pdf([path], output)
        self.assertTrue(report["pages"][0]["has_alpha"])
        with pymupdf.open(output) as document:
            self.assertGreater(document[0].get_images()[0][1], 0)
        with self.render(output) as rendered:
            self.assertEqual(rendered.getpixel((5, 10)), (255, 255, 255))
            self.assertEqual(rendered.getpixel((25, 10)), (0, 0, 255))
            for actual, expected in zip(rendered.getpixel((15, 10)), (255, 127, 127)):
                self.assertLessEqual(abs(actual - expected), 1)

    def test_opaque_alpha_channel_still_preserved(self):
        path = self.fixture(mode="RGBA", color=(50, 100, 200, 255))
        result = images_to_pdf([path], self.root / "opaque.pdf")
        self.assertTrue(result["pages"][0]["has_alpha"])

    def test_fully_transparent_alpha_preserved(self):
        path = self.fixture(mode="RGBA", color=(50, 100, 200, 0))
        output = self.root / "transparent.pdf"
        result = images_to_pdf([path], output)
        self.assertTrue(result["pages"][0]["has_alpha"])
        with self.render(output) as rendered:
            self.assertEqual(set(rendered.get_flattened_data()), {(255, 255, 255)})

    def test_grayscale_alpha_preserved(self):
        path = self.fixture(mode="LA", color=(0, 128))
        output = self.root / "grayscale-alpha.pdf"
        result = images_to_pdf([path], output)
        self.assertTrue(result["pages"][0]["has_alpha"])
        with self.render(output) as rendered:
            self.assertEqual(rendered.getpixel((10, 10)), (126, 126, 126))

    def test_tiny_pages_keep_exact_physical_dimensions(self):
        path = self.fixture(size=(1, 1))
        for dpi in (36, 144, 300):
            output = self.root / f"tiny-{dpi}.pdf"
            result = images_to_pdf([path], output, dpi=dpi)
            with pymupdf.open(output) as document:
                self.assertAlmostEqual(document[0].rect.width, 72 / dpi, places=5)
                self.assertAlmostEqual(document[0].rect.height, 72 / dpi, places=5)
            with self.render(output, dpi=dpi) as rendered:
                self.assertEqual(rendered.size, (1, 1))
                self.assertEqual(rendered.getpixel((0, 0)), (220, 30, 50))
            self.assertIn("No OCR", result["warnings"][0])

    def test_missing_pillow_error(self):
        path = self.fixture()
        import builtins
        real_import = builtins.__import__
        def missing(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("test unavailable Pillow")
            return real_import(name, *args, **kwargs)
        with mock.patch.object(builtins, "__import__", side_effect=missing):
            with self.assertRaisesRegex(JobError, "installed Pillow"):
                inspect_image(path)

    def test_palette_transparency_is_preserved(self):
        path = self.root / "palette.png"
        with Image.new("P", (20, 10), 0) as source:
            source.putpalette([255, 0, 0, 0, 0, 255] + [0] * 762)
            source.paste(1, (10, 0, 20, 10))
            source.save(path, transparency=0)
        output = self.root / "palette.pdf"
        result = images_to_pdf([path], output)
        self.assertTrue(result["pages"][0]["has_alpha"])
        with self.render(output) as rendered:
            self.assertEqual(rendered.getpixel((5, 5)), (255, 255, 255))
            self.assertEqual(rendered.getpixel((15, 5)), (0, 0, 255))

    def test_cmyk_warning_and_rendered_conversion(self):
        path = self.fixture("cmyk.jpg", mode="CMYK", color=(0, 255, 255, 0))
        output = self.root / "cmyk.pdf"
        result = images_to_pdf([path], output)
        self.assertTrue(any("CMYK converted" in warning for warning in result["warnings"]))
        with Image.open(path) as source, self.render(output) as rendered:
            self.assertEqual(rendered.tobytes(), source.convert("RGB").tobytes())

    def test_high_depth_grayscale_png_normalization(self):
        path = self.root / "depth.png"
        data = struct.pack("<HHH", 0, 32768, 65535)
        with Image.frombytes("I;16", (3, 1), data) as source:
            source.save(path)
        output = self.root / "depth.pdf"
        report = images_to_pdf([path], output)
        self.assertEqual(report["pages"][0]["source_bit_depth"], 16)
        self.assertTrue(any("precision is reduced" in warning for warning in report["warnings"]))
        with self.render(output) as rendered:
            values = [rendered.getpixel((x, 0))[0] for x in range(3)]
            self.assertEqual(values[0], 0)
            self.assertIn(values[1], (127, 128))
            self.assertEqual(values[2], 255)

    def test_high_depth_grayscale_jpeg2000_normalization(self):
        path = self.root / "depth.jp2"
        data = struct.pack("<HHH", 0, 32768, 65535)
        with Image.frombytes("I;16", (3, 1), data) as source:
            source.save(path)
        output = self.root / "depth-jp2.pdf"
        report = images_to_pdf([path], output)
        self.assertEqual(report["pages"][0]["source_bit_depth"], 16)
        with self.render(output) as rendered:
            self.assertEqual(rendered.getpixel((0, 0)), (0, 0, 0))
            self.assertEqual(rendered.getpixel((2, 0)), (255, 255, 255))

    def test_source_metadata_not_copied_to_pdf(self):
        path = self.root / "metadata.png"
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "PRIVATE-COMMENT-923745")
        exif = Image.Exif()
        exif[270] = "PRIVATE-EXIF-528734"
        with Image.new("RGB", (32, 20), "green") as source:
            source.save(path, pnginfo=info, exif=exif, icc_profile=b"PRIVATE-PROFILE-137649")
        before = path.read_bytes()
        output = self.root / "metadata.pdf"
        result = images_to_pdf([path], output)
        self.assertTrue(any("ICC profile removed" in warning for warning in result["warnings"]))
        with pymupdf.open(output) as document:
            for xref in range(1, document.xref_length()):
                obj = document.xref_object(xref).encode()
                stream = document.xref_stream(xref) or b""
                for secret in (b"PRIVATE-COMMENT-923745", b"PRIVATE-EXIF-528734", b"PRIVATE-PROFILE-137649"):
                    self.assertNotIn(secret, obj + stream)
        self.assertEqual(path.read_bytes(), before)

    def test_strict_truncation_setting_restored(self):
        path = self.fixture()
        previous = ImageFile.LOAD_TRUNCATED_IMAGES
        try:
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            original = images._decode
            def check(*args):
                self.assertFalse(ImageFile.LOAD_TRUNCATED_IMAGES)
                return original(*args)
            with mock.patch.object(images, "_decode", side_effect=check):
                inspect_image(path)
            self.assertTrue(ImageFile.LOAD_TRUNCATED_IMAGES)
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous

    def test_unavailable_codec_error(self):
        for suffix, codec in ((".png", "zlib"), (".jpg", "jpg"), (".jp2", "jpg_2000")):
            path = self.fixture("codec" + suffix)
            real_check = features.check_codec
            with mock.patch.object(features, "check_codec", side_effect=lambda name: False if name == codec else real_check(name)):
                with self.subTest(codec=codec), self.assertRaisesRegex(JobError, f"Pillow {codec} codec"):
                    inspect_image(path)


if __name__ == "__main__":
    unittest.main()
