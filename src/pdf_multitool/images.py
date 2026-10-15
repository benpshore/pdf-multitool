"""Bounded JPEG, PNG and single-image JPEG2000 intake and PDF conversion.

Run parsing in the normal time/memory-limited worker. Dimensions and cumulative
pixel budgets are checked before any pixel decoding. Pillow and PyMuPDF imports
are intentionally lazy so capability discovery need not load either engine.
"""

from __future__ import annotations

import math
import os
import stat
import struct
import tempfile
import threading
import warnings
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from .core import JobError

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".jp2", ".jpx", ".j2k"})
_FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".jp2": "JPEG2000",
    ".jpx": "JPEG2000",
    ".j2k": "JPEG2000",
}
_LOCK = threading.RLock()


@dataclass(frozen=True)
class ImageLimits:
    max_pixels_per_image: int = 40_000_000
    max_total_pixels: int = 200_000_000
    max_side: int = 16_000
    max_files: int = 200
    max_file_bytes: int = 50 * 1024 * 1024

    def __post_init__(self):
        for key, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{key} must be a positive integer")


DEFAULT_IMAGELIMITS = ImageLimits()


def _dependencies():
    try:
        from PIL import Image, ImageFile, ImageOps
    except ImportError as exc:
        raise JobError("Image processing needs the installed Pillow image codecs") from exc
    return Image, ImageFile, ImageOps


@contextmanager
def _safety():
    Image, ImageFile, ImageOps = _dependencies()
    # These settings are process-global; the supervisor isolates each job and the
    # lock also serializes this module's direct in-process callers.
    with _LOCK, warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        previous = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        try:
            yield Image, ImageOps
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous


def _require_codec(format_name):
    from PIL import features

    codec = {"JPEG": "jpg", "JPEG2000": "jpg_2000", "PNG": "zlib"}[format_name]
    if not features.check_codec(codec):
        raise JobError(f"{format_name} processing needs the installed Pillow {codec} codec")


def _no_symlinks(path: Path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise JobError("Image source and output paths must not contain symlinks")


def _stamp(fd):
    value = os.fstat(fd)
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


@dataclass
class _Source:
    path: Path
    fd: int
    stamp: tuple
    metadata: dict | None = None

    def unchanged(self):
        if _stamp(self.fd) != self.stamp:
            raise JobError(f"Image changed during processing: {self.path.name}")

    def close(self):
        os.close(self.fd)


@contextmanager
def _reader(source):
    # A duplicate prevents Pillow closing the descriptor held across preflight,
    # verification and decode. Reads are sequential, not concurrent.
    with os.fdopen(os.dup(source.fd), "rb") as file:
        file.seek(0)
        yield file


def _open_source(path, limits):
    path = Path(path).absolute()
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise JobError("Supported image suffixes are .jpg, .jpeg, .png, .jp2, .jpx and .j2k")
    _no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise JobError("Image source must be a regular file")
        if not 0 < info.st_size <= limits.max_file_bytes:
            raise JobError("Image input-byte limit exceeded or source is empty")
        return _Source(path, fd, _stamp(fd))
    except BaseException:
        os.close(fd)
        raise


def _read_exact(file, length):
    data = file.read(length)
    if len(data) != length:
        raise JobError("Truncated image structure")
    return data


def _j2k_framing(file, offset, length):
    """Require exactly one codestream, following marker and tile-part lengths."""
    end, position, tile_end = offset + length, offset + 2, None
    while position < end:
        file.seek(position)
        marker = _read_exact(file, 2)
        if marker == b"\xff\xd9":
            if position + 2 != end:
                raise JobError("JPEG2000 contains trailing data or multiple images")
            return
        if marker == b"\xff\x93":  # Start of entropy-coded tile data.
            if tile_end is None:
                raise JobError("Invalid JPEG2000 tile framing")
            if tile_end:
                position = tile_end
            else:
                # A zero Psot means the final tile-part extends to EOC. In
                # entropy data FF D9 cannot be a literal unescaped byte pair.
                data = _read_exact(file, end - position - 2)
                eoc = data.find(b"\xff\xd9")
                if eoc < 0:
                    raise JobError("Truncated JPEG2000 tile data")
                position += 2 + eoc
            tile_end = None
            continue
        if marker[:1] != b"\xff" or marker in {b"\xff\x4f", b"\xff\x92"}:
            raise JobError("Invalid JPEG2000 marker or multiple images")
        segment = struct.unpack(">H", _read_exact(file, 2))[0]
        if segment < 2 or position + 2 + segment > end:
            raise JobError("Truncated JPEG2000 marker segment")
        if marker == b"\xff\x90":  # SOT's Psot includes SOT/header/data.
            if segment != 10 or tile_end is not None:
                raise JobError("Invalid JPEG2000 tile-part header")
            body = _read_exact(file, 8)
            tile_length = struct.unpack(">I", body[2:6])[0]
            if tile_length and (tile_length < 14 or position + tile_length > end - 2):
                raise JobError("Truncated or invalid JPEG2000 tile-part length")
            tile_end = position + tile_length if tile_length else 0
        position += 2 + segment
    raise JobError("Truncated JPEG2000 codestream (missing end marker)")


def _j2k_precision(file, offset, length):
    if length < 45:
        raise JobError("Truncated JPEG2000 codestream")
    file.seek(offset)
    if _read_exact(file, 4) != b"\xff\x4f\xff\x51":
        raise JobError("Unsupported JPEG2000 codestream header")
    size = struct.unpack(">H", _read_exact(file, 2))[0]
    if size < 41 or size > length - 4:
        raise JobError("Invalid JPEG2000 size marker")
    data = _read_exact(file, size - 2)
    count = struct.unpack(">H", data[34:36])[0]
    if not 1 <= count <= 4 or size != 38 + 3 * count:
        raise JobError("Unsupported JPEG2000 component layout")
    precisions = [(data[36 + 3 * index] & 127) + 1 for index in range(count)]
    if any(data[36 + 3 * index] & 128 for index in range(count)):
        raise JobError("Signed JPEG2000 components are not supported")
    if len(set(precisions)) != 1 or max(precisions) > 16:
        raise JobError("Unsupported JPEG2000 component bit depth")
    file.seek(offset + length - 2)
    if _read_exact(file, 2) != b"\xff\xd9":
        raise JobError("Truncated JPEG2000 codestream (missing end marker)")
    _j2k_framing(file, offset, length)
    return precisions[0]


def _jpeg_framing(file, size):
    """Follow markers, skipping length-delimited metadata and stuffed scan bytes."""
    file.seek(0)
    data = _read_exact(file, size)
    position, saw_scan = 2, False
    while position < size:
        if data[position] != 255:
            raise JobError("Invalid JPEG marker framing")
        while position < size and data[position] == 255:
            position += 1
        if position >= size:
            raise JobError("Truncated JPEG marker")
        marker = data[position]
        position += 1
        if marker == 0xD9:
            if position != size:
                raise JobError("JPEG contains trailing data or multiple images")
            if not saw_scan:
                raise JobError("JPEG has no image scan")
            return
        if marker in {0, 0xD8} or 0xD0 <= marker <= 0xD7:
            raise JobError("Invalid JPEG marker or multiple images")
        if marker == 1:
            continue
        if position + 2 > size:
            raise JobError("Truncated JPEG segment")
        length = struct.unpack(">H", data[position : position + 2])[0]
        if length < 2 or position + length > size:
            raise JobError("Truncated or invalid JPEG segment")
        position += length
        if marker == 0xDA:
            saw_scan = True
            while True:
                start = data.find(b"\xff", position)
                if start < 0:
                    raise JobError("Truncated JPEG scan (missing end marker)")
                position = start + 1
                while position < size and data[position] == 255:
                    position += 1
                if position >= size:
                    raise JobError("Truncated JPEG scan marker")
                if data[position] in {0, 1} or 0xD0 <= data[position] <= 0xD7:
                    position += 1
                    continue
                position = start
                break
    raise JobError("Truncated JPEG (missing end marker)")


def _structure(source):
    """Validate framing without allocating pixel buffers or trusting the suffix."""
    with _reader(source) as file:
        head = file.read(32)
        size = source.stamp[2]
        if head.startswith(b"\xff\xd8\xff"):
            file.seek(-2, os.SEEK_END)
            if file.read(2) != b"\xff\xd9":
                raise JobError("Truncated JPEG (missing end marker)")
            _jpeg_framing(file, size)
            return "JPEG", 8, None
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            position, chunks, bit_depth, exif = 8, 0, None, None
            while position < size:
                chunks += 1
                if chunks > 100_000:
                    raise JobError("PNG chunk-count limit exceeded")
                file.seek(position)
                length, kind = struct.unpack(">I4s", _read_exact(file, 8))
                if length > 0x7FFFFFFF or position + 12 + length > size:
                    raise JobError("Truncated or invalid PNG chunk")
                if chunks == 1:
                    if kind != b"IHDR" or length != 13:
                        raise JobError("Invalid PNG header")
                    bit_depth = _read_exact(file, 13)[8]
                elif kind == b"IHDR":
                    raise JobError("Duplicate PNG header")
                if kind == b"eXIf":
                    if exif is not None:
                        raise JobError("Duplicate PNG EXIF metadata")
                    exif = b"Exif\0\0" + _read_exact(file, length)
                if kind in {b"acTL", b"fcTL", b"fdAT"}:
                    raise JobError("Animated or multiframe PNG is not supported")
                position += 12 + length
                if kind == b"IEND":
                    if length or position != size:
                        raise JobError("Invalid PNG end marker or trailing data")
                    return "PNG", bit_depth, exif
            raise JobError("Truncated PNG (missing end marker)")
        if head.startswith(b"\xff\x4f\xff\x51"):
            return "JPEG2000", _j2k_precision(file, 0, size), None
        if head.startswith(b"\0\0\0\x0cjP  \r\n\x87\n"):
            position, count, codestream, brand = 0, 0, None, None
            while position < size:
                count += 1
                if count > 100_000:
                    raise JobError("JPEG2000 box-count limit exceeded")
                file.seek(position)
                length, kind = struct.unpack(">I4s", _read_exact(file, 8))
                header = 8
                if length == 1:
                    length = struct.unpack(">Q", _read_exact(file, 8))[0]
                    header = 16
                elif length == 0:
                    length = size - position
                if length < header or position + length > size:
                    raise JobError("Truncated or invalid JPEG2000 box")
                if kind == b"ftyp":
                    if brand is not None or length < header + 8:
                        raise JobError("Invalid JPEG2000 file-type box")
                    brand = _read_exact(file, 4)
                    if brand != b"jp2 ":
                        raise JobError("Layered/extended JPX containers are not supported")
                if kind in {b"comp", b"jplh", b"jpch"}:
                    raise JobError("Layered or multiframe JPEG2000 is not supported")
                if kind == b"jp2c":
                    if codestream is not None:
                        raise JobError("Multiframe JPEG2000 is not supported")
                    codestream = (position + header, length - header)
                position += length
            if codestream is None or brand is None:
                raise JobError("Invalid JPEG2000 container")
            return "JPEG2000", _j2k_precision(file, *codestream), None
    raise JobError("Unsupported or unrecognized image content; expected JPEG, PNG or JPEG2000")


@contextmanager
def _image(source, Image):
    with _reader(source) as file, Image.open(file, formats=["JPEG", "PNG", "JPEG2000"]) as image:
        yield image


def _preflight(source, limits, Image):
    format_name, bit_depth, exif = _structure(source)
    if format_name != _FORMATS[source.path.suffix.lower()]:
        raise JobError("Image suffix does not match its actual content")
    _require_codec(format_name)
    with _image(source, Image) as image:
        if image.format != format_name:
            raise JobError("Unsupported image format or multiframe image")
        width, height = image.size
        if min(width, height) < 1 or max(width, height) > limits.max_side:
            raise JobError("Image dimension limit exceeded")
        pixels = width * height
        if pixels > limits.max_pixels_per_image:
            raise JobError("Per-image pixel limit exceeded")
        frames = getattr(image, "n_frames", 1)
        if frames != 1 or getattr(image, "is_animated", False):
            raise JobError("Animated or multiframe images are not supported")
        if exif is not None:
            image.info["exif"] = exif
        # PNG overrides getexif() to load pixels when eXIf is absent or after
        # IDAT. The structural scan has collected eXIf already; call the base
        # method to guarantee all-file pixel preflight remains decode-free.
        orientation = Image.Image.getexif(image).get(274, 1)
        if (
            isinstance(orientation, bool)
            or not isinstance(orientation, int)
            or not 1 <= orientation <= 8
        ):
            raise JobError("Invalid EXIF orientation")
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        mode = "RGBA" if has_alpha else "RGB"
        notices = []
        if format_name == "JPEG":
            notices.append("JPEG source is lossy; PDF embedding adds no further lossy compression.")
        if format_name == "JPEG2000":
            notices.append(
                "JPEG2000 source may already be lossy; decoded pixels are embedded losslessly."
            )
        if image.mode == "CMYK":
            notices.append(
                "CMYK converted to RGB using Pillow; exact print colors are not preserved."
            )
        if image.info.get("icc_profile"):
            notices.append(
                "Embedded ICC profile removed without color-managed conversion; colors may differ."
            )
        if format_name == "PNG" and any(key in image.info for key in ("gamma", "chromaticity")):
            notices.append(
                "PNG gamma/chromaticity metadata is not applied; decoded color values are retained."
            )
        if bit_depth > 8:
            notices.append(
                f"{bit_depth}-bit image samples normalized to 8-bit {mode}"
                f"; high-depth precision is reduced."
            )
        if orientation != 1:
            notices.append(f"EXIF orientation {orientation} applied to the image pixels.")
        if image.mode not in {"RGB", "RGBA", "CMYK"}:
            notices.append(f"Image mode {image.mode} normalized to {mode}.")
        metadata = {
            "name": source.path.name,
            "format": format_name,
            "input_bytes": source.stamp[2],
            "source_width": width,
            "source_height": height,
            "width": height if orientation in {5, 6, 7, 8} else width,
            "height": width if orientation in {5, 6, 7, 8} else height,
            "pixels": pixels,
            "frames": 1,
            "orientation": orientation,
            "source_mode": image.mode,
            "mode": mode,
            "source_bit_depth": bit_depth,
            "has_alpha": has_alpha,
            "image_only": True,
            "ocr_performed": False,
            "warnings": notices,
        }
    source.unchanged()
    source.metadata = metadata
    return metadata


def _decode(source, Image, ImageOps):
    source.unchanged()
    with _image(source, Image) as image:
        image.verify()
    with _image(source, Image) as image:
        image.load()  # Strict decoding, with truncated-image acceptance disabled.
        metadata = source.metadata
        # Use the orientation captured without decoding during preflight,
        # including an eXIf chunk located after PNG image-data chunks.
        image.getexif()[274] = metadata["orientation"]
        ImageOps.exif_transpose(image, in_place=True)
        if image.size != (metadata["width"], metadata["height"]):
            raise JobError("Image dimensions changed during decoding")
        # A fresh image excludes EXIF, ICC, comments, GPS, filenames and other
        # source metadata from the raster stream that goes into the PDF.
        normalized = Image.new(metadata["mode"], image.size)
        converted = None
        try:
            if image.mode in {"I", "I;16", "I;16B", "I;16L"} and metadata["source_bit_depth"] > 8:
                scale = 255.0 / ((1 << metadata["source_bit_depth"]) - 1)
                with image.convert("I") as wide, wide.point(lambda value: value * scale) as scaled:
                    converted = scaled.convert("L")
            elif image.mode != metadata["mode"]:
                converted = image.convert(metadata["mode"])
            normalized.paste(converted if converted is not None else image)
            source.unchanged()
            return normalized
        except BaseException:
            normalized.close()
            raise
        finally:
            if converted is not None:
                converted.close()


def inspect_image(path, limits: ImageLimits = DEFAULT_IMAGELIMITS) -> dict:
    """Validate a supported image and return JSON-compatible normalized metadata.

    This performs strict verification and decode, not merely extension/header
    identification. Original files are never modified; no OCR is performed.
    """
    source = None
    try:
        with _safety() as (Image, ImageOps):
            source = _open_source(path, limits)
            metadata = _preflight(source, limits, Image)
            if metadata["pixels"] > limits.max_total_pixels:
                raise JobError("Total image pixel limit exceeded")
            with _decode(source, Image, ImageOps):
                pass
            return metadata
    except JobError:
        raise
    except Exception as exc:
        raise JobError(f"Image validation failed: {exc}") from exc
    finally:
        if source is not None:
            source.close()


def _output_path(output):
    output = Path(output).absolute()
    _no_symlinks(output)
    if os.path.lexists(output):
        raise JobError("Refusing to overwrite an existing image-PDF destination")
    if not output.parent.is_dir():
        raise JobError("Image-PDF destination directory does not exist")
    return output


def _verify_pdf(path, pages, pymupdf):
    with pymupdf.open(path) as document:
        if not document.is_pdf or len(document) != len(pages):
            raise JobError("Generated image PDF failed page-count verification")
        for page, expected in zip(document, pages, strict=True):
            if (
                abs(page.rect.width - expected["page_width_points"]) > 0.01
                or abs(page.rect.height - expected["page_height_points"]) > 0.01
            ):
                raise JobError("Generated image PDF failed page-size verification")
            images = page.get_images(full=True)
            if len(images) != 1 or images[0][2:4] != (expected["width"], expected["height"]):
                raise JobError("Generated image PDF failed raster-dimension verification")
            if bool(images[0][1]) != expected["has_alpha"]:
                raise JobError("Generated image PDF failed alpha-channel verification")


def images_to_pdf(paths, output, dpi=144, limits: ImageLimits = DEFAULT_IMAGELIMITS) -> dict:
    """One page per image, in order, using lossless normalized RGB/RGBA rasters.

    Explicit DPI sets page dimensions and must be finite within 36..300; embedded
    DPI is ignored. All inputs preflight before any decode. Existing destinations
    are never overwritten, and partial temporary output is removed on failure.
    """
    sources, temporary = [], None
    try:
        if (
            isinstance(dpi, bool)
            or not isinstance(dpi, (int, float))
            or not math.isfinite(dpi)
            or not 36 <= dpi <= 300
        ):
            raise JobError("Image DPI must be a finite number from 36 through 300")
        if isinstance(paths, (str, bytes, os.PathLike)):
            raise JobError("Provide an ordered collection of image paths")
        selected = []
        for path in paths:
            selected.append(path)
            if len(selected) > limits.max_files:
                raise JobError("Image file-count limit exceeded")
        if not selected:
            raise JobError("Select at least one image")
        destination = _output_path(output)
        with _safety() as (Image, ImageOps):
            total_pixels = 0
            for path in selected:
                source = _open_source(path, limits)
                sources.append(source)
                metadata = _preflight(source, limits, Image)
                total_pixels += metadata["pixels"]
                if total_pixels > limits.max_total_pixels:
                    raise JobError("Total image pixel limit exceeded")
            _require_codec("PNG")
            import io

            import pymupdf

            fd, temporary = tempfile.mkstemp(
                prefix=".image-pdf-", suffix=".tmp", dir=destination.parent
            )
            os.close(fd)
            pages = []
            with pymupdf.open() as document:
                for number, source in enumerate(sources, 1):
                    metadata = dict(source.metadata)
                    metadata.update(
                        page=number,
                        page_width_points=metadata["width"] * 72.0 / dpi,
                        page_height_points=metadata["height"] * 72.0 / dpi,
                    )
                    with _decode(source, Image, ImageOps) as normalized, io.BytesIO() as encoded:
                        normalized.save(encoded, format="PNG")
                        if min(metadata["page_width_points"], metadata["page_height_points"]) < 1:
                            # MuPDF clamps a MediaBox side below 1 user-space unit.
                            # A positive explicit UserUnit preserves tiny images'
                            # exact physical dimensions without resizing pixels.
                            page = document.new_page(
                                width=metadata["width"], height=metadata["height"]
                            )
                            document.xref_set_key(page.xref, "UserUnit", str(72.0 / dpi))
                            page = document.reload_page(page)
                        else:
                            page = document.new_page(
                                width=metadata["page_width_points"],
                                height=metadata["page_height_points"],
                            )
                        page.draw_rect(page.rect, color=None, fill=(1, 1, 1), overlay=False)
                        page.insert_image(
                            page.rect, stream=encoded.getvalue(), keep_proportion=False
                        )
                    pages.append(metadata)
                document.set_metadata(
                    {"creator": "PDF Multitool", "producer": "Local image normalization"}
                )
                document.save(temporary, garbage=3, deflate=True)
            _verify_pdf(temporary, pages, pymupdf)
            for source in sources:
                source.unchanged()
            report = {
                "page_count": len(pages),
                "pages": pages,
                "dpi": float(dpi),
                "total_pixels": total_pixels,
                "formats": sorted({p["format"] for p in pages}),
                "image_only": True,
                "ocr_performed": False,
                "warnings": [
                    (
                        "No OCR is performed; source metadata is stripped from generated "
                        "raster streams."
                    )
                ]
                + [f"{p['name']}: {warning}" for p in pages for warning in p["warnings"]],
                "verified": {
                    "page_count": True,
                    "page_dimensions": True,
                    "raster_dimensions": True,
                    "alpha_channels": True,
                },
            }
            with open(temporary, "rb") as file:
                os.fsync(file.fileno())
            _no_symlinks(destination)
            # Hard-link publication is atomic and fails if the destination was
            # created after our check. Unlike replace()/rename(), it cannot clobber.
            os.link(temporary, destination)
            return report
    except JobError:
        raise
    except Exception as exc:
        raise JobError(f"Image PDF conversion failed: {exc}") from exc
    finally:
        for source in sources:
            source.close()
        if temporary is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
