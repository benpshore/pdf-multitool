"""Pinned PDF Oxide rendering, Office conversion and image construction.

Native parsing runs only in the existing disposable, bounded worker. Exported
office files and rendered pixels are derivatives, not fidelity certifications.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .core import JobError, safe_name
from .oxide_runtime import (
    native,
    open_source,
    read_input,
    require_runtime,
    selected,
    validate_pdf_bytes,
    write_bytes,
    write_json,
)

MAX_PIXELS = 16_000_000
MAX_TOTAL_PIXELS = 64_000_000
MAX_IMAGE_BYTES = 16 * 1024**2
MAX_IMAGES = 100
MAX_PLATES = 32
MAX_OFFICE_PAGES = 250
MAX_ZIP_ENTRIES = 2000
MAX_ZIP_BYTES = 64 * 1024**2
MAX_ZIP_MEMBER_BYTES = 16 * 1024**2
MAX_XML_NODES = 200_000
MAX_XML_TEXT = 2_000_000
MAX_OFFICE_CELLS = 50_000
OFFICE_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
PDF_VALIDATION = {
    "native_reopen": True,
    "independent_parser": "PyMuPDF",
    "page_count_and_rectangles_checked": True,
    "content_fidelity_checked": False,
}


def _field(label, kind="string", default="", **kwargs):
    return {"label": label, "type": kind, "default": default, **kwargs}


OPERATIONS = {
    "oxide_flatten": {
        "label": "Rasterize PDF with PDF Oxide",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": False,
        "description": (
            "Rasterize with native default paper and margins, or explicitly preserve the visible "
            "page size using PDF Oxide rendering and its native page builder. Text, forms, links "
            "and signatures lose their interactive structure. This is not verified redaction."
        ),
        "outputs": ["application/pdf", "application/json"],
        "options": {
            "layout": _field(
                "Page layout",
                default="native_default",
                enum=["native_default", "preserve_visible_page"],
            ),
            "dpi": _field("Resolution (DPI)", "integer", 72, minimum=36, maximum=300),
            "allow_rasterization": _field(
                "Allow loss of text and interactive structure", "boolean", False
            ),
            "allow_page_relayout": _field("Allow default paper and margins", "boolean", False),
        },
    },
    "oxide_render": {
        "label": "Render with PDF Oxide",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": True,
        "description": (
            "Render selected pages as PNG, JPEG, raw premultiplied pixels or ink plates. "
            "Resolution is bounded; compare each derivative with its source."
        ),
        "outputs": ["image/png", "image/jpeg", "application/octet-stream", "application/json"],
        "options": {
            "format": _field(
                "Output", default="png", enum=["png", "jpeg", "pixmap", "separations", "separation"]
            ),
            "sizing": _field("Size", default="dpi", enum=["dpi", "fit"]),
            "dpi": _field("Resolution (DPI)", "integer", 72, minimum=36, maximum=300),
            "width": _field("Fit width (pixels)", "integer", 1024, minimum=1, maximum=4096),
            "height": _field("Fit height (pixels)", "integer", 1024, minimum=1, maximum=4096),
            "background": _field("Background RGBA", default="[1,1,1,1]", maxLength=100),
            "transparent": _field("Transparent background", "boolean", False),
            "annotations": _field("Render annotations", "boolean", True),
            "jpeg_quality": _field("JPEG quality", "integer", 85, minimum=1, maximum=100),
            "excluded_layers": _field("Hidden layer names (JSON)", default="[]", maxLength=8192),
            "ink": _field("Single ink name", default="Black", maxLength=256),
        },
    },
    "oxide_office_export": {
        "label": "PDF to Office with PDF Oxide",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": False,
        "description": (
            "Convert all pages to DOCX, XLSX or PPTX. Layout, tables, images and reading "
            "order may change. Positioned exports do not guarantee real spreadsheet cells. "
            "Sources above 30 pages (DOCX/PPTX) or 200 pages (XLSX) use upstream flow mode."
        ),
        "outputs": [*OFFICE_TYPES.values(), "application/json"],
        "options": {
            "format": _field("Office format", default="docx", enum=list(OFFICE_TYPES)),
        },
    },
    "oxide_office_import": {
        "label": "Office to PDF with PDF Oxide",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": False,
        "description": (
            "Convert bounded DOCX, XLSX or PPTX files to PDF. Macros, embedded objects, "
            "external relationships and encrypted Office inputs are rejected. "
            "Compare text, numbers, tables and fonts with the original."
        ),
        "outputs": ["application/pdf", "application/json"],
        "options": {
            "format": _field("Input format", default="auto", enum=["auto", *OFFICE_TYPES]),
        },
    },
    "oxide_image_pdf": {
        "label": "Image to PDF with PDF Oxide",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": False,
        "description": (
            "Create one PDF per bounded PNG or JPEG, centered on native default paper with "
            "margins. CMYK needs explicit RGB normalization. Source files are preserved."
        ),
        "outputs": ["application/pdf", "application/json"],
        "options": {
            "normalize_cmyk": _field(
                "Convert CMYK images to RGB in the derivative", "boolean", False
            ),
        },
    },
    "oxide_images_pdf": {
        "label": "Combine images with PDF Oxide",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": False,
        "description": (
            "Center each PNG or JPEG on native default paper with margins, in input order. "
            "At most 100 images and 64 million decoded pixels per document."
        ),
        "outputs": ["application/pdf", "application/json"],
        "options": {
            "normalize_cmyk": _field(
                "Convert CMYK images to RGB in the derivative", "boolean", False
            ),
        },
    },
}


def _json_option(value, label):
    try:
        return json.loads(value)
    except ValueError, RecursionError:
        raise JobError(f"{label} must be valid JSON") from None


def validate_options(operation, options):
    """Cross-field checks, also called by the parent before spawning a worker."""
    if operation == "oxide_flatten":
        if not options["allow_rasterization"]:
            raise JobError("Enable allow_rasterization to create an image-only derivative")
        if options["layout"] == "native_default" and not options["allow_page_relayout"]:
            raise JobError(
                "Native flattening changes page dimensions and margins; enable allow_page_relayout"
            )
    elif operation == "oxide_render":
        background = _json_option(options["background"], "background")
        if not (
            isinstance(background, list)
            and len(background) == 4
            and all(
                type(v) in {int, float} and math.isfinite(v) and 0 <= v <= 1 for v in background
            )
        ):
            raise JobError("background must contain four finite RGBA values between 0 and 1")
        layers = _json_option(options["excluded_layers"], "excluded_layers")
        if not (
            isinstance(layers, list)
            and len(layers) <= 128
            and all(isinstance(v, str) and 0 < len(v) <= 256 for v in layers)
        ):
            raise JobError("excluded_layers must contain at most 128 bounded layer names")
        if options["sizing"] == "fit" and options["width"] * options["height"] > MAX_PIXELS:
            raise JobError("Render target exceeds the 16-million-pixel limit")
        if options["format"] == "jpeg" and (options["transparent"] or background[3] != 1):
            raise JobError("JPEG cannot preserve a transparent background; choose PNG")
        if options["format"] not in {"png", "jpeg"} and (
            options["sizing"] != "dpi"
            or options["transparent"]
            or background != [1, 1, 1, 1]
            or not options["annotations"]
            or layers
        ):
            raise JobError("Raw pixels and ink plates expose only page and DPI options upstream")
        if options["format"] == "separation" and not options["ink"].strip():
            raise JobError("A nonempty ink name is required")
    elif options.get("password") and operation != "oxide_office_export":
        raise JobError("Encrypted Office and image inputs are unsupported")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _read_source(request, index=0):
    data = read_input(request, index=index)
    return data, {"name": safe_name(request["names"][index]), "sha256": _hash(data)}


def _pixel_size(width, height):
    if type(width) is not int or type(height) is not int or min(width, height) <= 0:
        raise JobError("Conversion returned invalid image dimensions")
    pixels = width * height
    if pixels > MAX_PIXELS:
        raise JobError("Image exceeds the 16-million-pixel limit")
    return pixels


def _page_extent(doc, page):
    media = native("page media box", lambda: doc.page_media_box(page))
    crop = native("page crop box", lambda: doc.page_crop_box(page))
    rotation = native("page rotation", lambda: doc.page_rotation(page))
    for box in (media, crop):
        if box is not None and (
            len(box) != 4 or not all(math.isfinite(v) and abs(v) <= 1_000_000 for v in box)
        ):
            raise JobError("Page bounds are invalid or exceed the conversion limit")
    if rotation % 90:
        raise JobError("Rendering requires a page rotation divisible by 90")
    # Pinned renderer uses the intersection if valid, otherwise its media box.
    box = media
    if crop:
        intersection = (
            max(media[0], crop[0]),
            max(media[1], crop[1]),
            min(media[2], crop[2]),
            min(media[3], crop[3]),
        )
        if intersection[2] > intersection[0] and intersection[3] > intersection[1]:
            box = intersection
    width, height = box[2] - box[0], box[3] - box[1]
    if min(width, height) <= 0:
        raise JobError("Page bounds have no positive area")
    return (height, width) if rotation % 180 else (width, height)


def _encoded_image(data, allowed=("PNG", "JPEG")):
    from PIL import Image

    if not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise JobError("Encoded image exceeds the 16-MiB limit")
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height, mode, kind = image.width, image.height, image.mode, image.format
            _pixel_size(width, height)
            if kind not in allowed or getattr(image, "n_frames", 1) != 1:
                raise JobError("Only single-frame PNG and JPEG images are supported")
            image.verify()
        # JPEG verify() alone does not decode the entropy stream. Validate a
        # complete bounded decode before handing bytes to a native constructor
        # that may embed the original stream without checking its pixels.
        with Image.open(io.BytesIO(data)) as decoded:
            decoded.load()
    except JobError:
        raise
    except Exception:
        raise JobError("Image metadata or encoded image validation failed") from None
    return {"width": width, "height": height, "mode": mode, "format": kind}


def _diagnostics(doc):
    records = native("conversion diagnostics", doc.structured_warnings)
    if len(records) > 2000 or len(json.dumps(records)) > 256 * 1024:
        raise JobError("Conversion diagnostics exceed the output limit")
    return records


def _diagnostic_status(records, pages, *, needs_text=False):
    """Classify loss reported by native code, preserving its original records.

    Native page numbers are zero-based; an unscoped warning applies to every
    selected page. A scan is valid raster input, so no_text_layer only makes a
    text-dependent Office conversion partial, not an otherwise intact render.
    """
    applicable = {
        record["category"]
        for record in records
        if record.get("page") is None or record["page"] in pages
    }
    truncated = "operator_cap_exceeded" in applicable
    ocr_required = True if needs_text and "no_text_layer" in applicable else None
    omitted = sorted(applicable & {"glyph_dropped", "image_suppressed"})
    return {
        "diagnostic_status": "warnings_present" if applicable else "none_reported",
        "truncated": truncated,
        "ocr_required": ocr_required,
        "omitted_content_categories": omitted,
        "partial": truncated or ocr_required is True or bool(omitted),
    }


def _diagnostic_warning(status):
    return (
        [
            "Partial conversion: PDF Oxide reported truncated or omitted content, or a "
            "missing text layer required by this conversion. Inspect structured_warnings."
        ]
        if status["partial"]
        else []
    )


def _render(request, options, add):
    doc, _, source = open_source(request, options)
    pages = selected(options, source["pages"])
    report = {
        "source": source,
        "engine": "pdf-oxide==0.3.78",
        "operation": "render",
        "settings": {key: value for key, value in options.items() if key != "password"},
        "fidelity_verified": False,
        "pages": [],
    }
    total_pixels = 0
    for page in pages:
        page_width, page_height = _page_extent(doc, page)
        if options["sizing"] == "dpi":
            width = math.ceil(page_width * options["dpi"] / 72)
            height = math.ceil(page_height * options["dpi"] / 72)
        else:
            scale = min(options["width"] / page_width, options["height"] / page_height)
            width, height = (
                max(1, math.ceil(page_width * scale)),
                max(1, math.ceil(page_height * scale)),
            )
        pixels = _pixel_size(width, height)
        mode = options["format"]
        names = []
        plate_budget = 1
        if mode in {"separations", "separation"}:
            names = native("page ink inventory", lambda page=page: doc.get_page_inks_deep(page))
            # Process CMYK inks can be used directly in a content stream without
            # a named ColorSpace resource, so the resource inventory omits them.
            plate_budget = len(set(names) | {"Cyan", "Magenta", "Yellow", "Black"})
            if plate_budget > MAX_PLATES:
                raise JobError("Page exceeds the 32-ink-plate limit")
        total_pixels += pixels * (plate_budget if mode == "separations" else 1)
        if total_pixels > MAX_TOTAL_PIXELS:
            raise JobError("Render exceeds the 64-million-pixel document limit")
        entry = {"page": page + 1, "source_sha256": source["sha256"], "outputs": []}
        prefix = f"page-{page + 1:04d}"
        if mode in {"png", "jpeg"}:
            kwargs = {
                "format": mode,
                "background": tuple(json.loads(options["background"])),
                "transparent": options["transparent"],
                "render_annotations": options["annotations"],
                "jpeg_quality": options["jpeg_quality"],
                "excluded_layers": json.loads(options["excluded_layers"]),
            }
            if options["sizing"] == "fit":
                data = native(
                    "page fit rendering",
                    lambda page=page, kwargs=kwargs: doc.render_page_fit(
                        page, options["width"], options["height"], **kwargs
                    ),
                )
            else:
                data = native(
                    "page rendering",
                    lambda page=page, kwargs=kwargs: doc.render_page(
                        page, dpi=options["dpi"], **kwargs
                    ),
                )
            data = bytes(data)
            info = _encoded_image(data)
            if options["sizing"] == "fit":
                if info["width"] > options["width"] or info["height"] > options["height"]:
                    raise JobError("Rendered image exceeds its requested fit box")
            elif (info["width"], info["height"]) != (width, height):
                raise JobError("Renderer changed the requested resolution; no output kept")
            filename = f"{prefix}.{mode}"
            write_bytes(request, add, filename, data)
            entry["outputs"].append({"file": filename, "sha256": _hash(data), **info})
        elif mode == "pixmap":
            pixmap = native(
                "raw pixel rendering", lambda page=page: doc.render_pixmap(page, dpi=options["dpi"])
            )
            _pixel_size(pixmap.width, pixmap.height)
            if (pixmap.width, pixmap.height) != (width, height) or len(pixmap.data) != pixels * 4:
                raise JobError("Raw pixel buffer dimensions or length are inconsistent")
            filename = f"{prefix}.rgba"
            write_bytes(request, add, filename, pixmap.data)
            entry["outputs"].append(
                {
                    "file": filename,
                    "sha256": _hash(pixmap.data),
                    "width": width,
                    "height": height,
                    "pixel_format": "premultiplied RGBA8888",
                    "row_stride": width * 4,
                }
            )
        else:
            plates = (
                native(
                    "ink separation rendering",
                    lambda page=page: doc.render_separations(page, dpi=options["dpi"]),
                )
                if mode == "separations"
                else [
                    native(
                        "single ink rendering",
                        lambda page=page: doc.render_separation(
                            page, options["ink"], dpi=options["dpi"]
                        ),
                    )
                ]
            )
            if len(plates) > MAX_PLATES or (mode == "separations" and len(plates) > plate_budget):
                raise JobError("Renderer returned more ink plates than the bounded inventory")
            for ordinal, plate in enumerate(plates, 1):
                if (plate.width, plate.height) != (width, height) or len(plate.data) != pixels:
                    raise JobError("Ink plate dimensions or buffer length are inconsistent")
                filename = f"{prefix}-ink-{ordinal:02d}.pgm"
                data = f"P5\n{width} {height}\n255\n".encode("ascii") + plate.data
                write_bytes(request, add, filename, data)
                entry["outputs"].append(
                    {
                        "file": filename,
                        "sha256": _hash(data),
                        "ink_name": plate.ink_name,
                        "width": width,
                        "height": height,
                        "pixel_meaning": (
                            "0=no ink; 255=full ink coverage; invert for black-on-white display"
                        ),
                    }
                )
            entry["native_ink_inventory"] = names
            entry["status"] = "plates_returned" if plates else "no_plates_returned"
        report["pages"].append(entry)
    records = _diagnostics(doc)
    report["structured_warnings"] = records
    status = _diagnostic_status(records, pages)
    report.update(status)
    for entry in report["pages"]:
        entry.update(_diagnostic_status(records, {entry["page"] - 1}))
    write_json(request, add, "render.json", report)
    return [
        "PDF Oxide rendering fidelity is unverified; compare every page with the source.",
        *_diagnostic_warning(status),
    ], status["partial"]


def _xml_budget(data, counters):
    from defusedxml.ElementTree import iterparse

    depth = 0
    try:
        for event, element in iterparse(io.BytesIO(data), events=("start", "end"), forbid_dtd=True):
            if event == "start":
                depth += 1
                counters["nodes"] += 1
                if depth > 128 or counters["nodes"] > MAX_XML_NODES:
                    raise JobError("Office XML exceeds its depth or node limit")
                local = element.tag.rsplit("}", 1)[-1].lower()
                if local in {"oleobject", "object", "control", "altchunk", "ddelink", "olelink"}:
                    raise JobError(
                        "Office embedded objects, controls and external content are unsupported"
                    )
                if local == "relationship":
                    target = unquote(element.attrib.get("Target", ""))
                    if (
                        element.attrib.get("TargetMode", "").lower() == "external"
                        or urlsplit(target).scheme
                        or urlsplit(target).netloc
                        or target.startswith(("/", "\\"))
                    ):
                        raise JobError("Office external relationships are unsupported")
                if local in {"default", "override"}:
                    kind = element.attrib.get("ContentType", "").lower()
                    if any(
                        word in kind for word in ("macroenabled", "vba", "oleobject", "activex")
                    ):
                        raise JobError("Office macro or active-content types are unsupported")
                if local in {"sheet", "sldid"}:
                    counters["pages"] += 1
                    if counters["pages"] > MAX_OFFICE_PAGES:
                        raise JobError("Office file exceeds its sheet or slide limit")
                if local in {"c", "tc"}:
                    counters["cells"] += 1
                    if counters["cells"] > MAX_OFFICE_CELLS:
                        raise JobError("Office file exceeds its cell limit")
                if local == "c" and "r" in element.attrib:
                    _cell_reference(element.attrib["r"])
                if local == "dimension" and "ref" in element.attrib:
                    for reference in element.attrib["ref"].split(":"):
                        _cell_reference(reference)
                if local == "row" and "r" in element.attrib:
                    number = element.attrib["r"]
                    if (
                        not number.isascii()
                        or not number.isdigit()
                        or len(number) > 6
                        or not 1 <= int(number) <= MAX_OFFICE_CELLS
                    ):
                        raise JobError("Office worksheet row index exceeds its limit")
            else:
                counters["text"] += len(element.text or "") + len(element.tail or "")
                if counters["text"] > MAX_XML_TEXT:
                    raise JobError("Office XML exceeds its text limit")
                depth -= 1
                element.clear()
    except JobError:
        raise
    except Exception:
        raise JobError("Office XML is malformed or contains forbidden DTD/entities") from None


def _cell_reference(value):
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,5})", value)
    if not match:
        raise JobError("Office worksheet contains an invalid or excessive cell reference")
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - ord("A") + 1
    if column > 1024 or int(match[2]) > MAX_OFFICE_CELLS:
        raise JobError("Office worksheet dimensions exceed their limit")


def office_preflight(data, kind):
    """Inspect every package entry without filesystem extraction or active content."""
    if kind not in OFFICE_TYPES:
        raise JobError("Only DOCX, XLSX and PPTX Office packages are supported")
    counters = {"nodes": 0, "text": 0, "cells": 0, "pages": 0, "image_pixels": 0, "images": 0}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ZIP_ENTRIES:
                raise JobError("Office archive exceeds its entry-count limit")
            names, folded_names, total = set(), set(), 0
            for entry in entries:
                name = entry.filename
                lower = name.lower()
                path = PurePosixPath(name)
                if (
                    name in names
                    or lower in folded_names
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in name
                    or "\x00" in name
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise JobError("Office archive contains an unsafe or duplicate entry name")
                names.add(name)
                folded_names.add(lower)
                if any(
                    word in lower
                    for word in (
                        "vbaproject",
                        "vbasignature",
                        "activex",
                        "/embeddings/",
                        "externallinks",
                        "macrosheets",
                    )
                ):
                    raise JobError(
                        "Office macros, embedded objects and external links are unsupported"
                    )
                if entry.flag_bits & 1 or entry.compress_type not in {
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                }:
                    raise JobError("Encrypted or unsupported Office archive compression")
                total += entry.file_size
                if (
                    entry.file_size > MAX_ZIP_MEMBER_BYTES
                    or total > MAX_ZIP_BYTES
                    or entry.file_size > max(1, entry.compress_size) * 200
                ):
                    raise JobError("Office archive exceeds its decompression limits")
                if entry.is_dir():
                    continue
                with archive.open(entry) as stream:
                    body = stream.read(MAX_ZIP_MEMBER_BYTES + 1)
                if len(body) != entry.file_size or len(body) > MAX_ZIP_MEMBER_BYTES:
                    raise JobError("Office archive member length is inconsistent")
                if lower.endswith((".xml", ".rels")):
                    _xml_budget(body, counters)
                elif "/media/" in lower or lower.endswith((".png", ".jpeg", ".jpg")):
                    info = _encoded_image(body)
                    counters["images"] += 1
                    counters["image_pixels"] += info["width"] * info["height"]
                    if (
                        counters["images"] > MAX_IMAGES
                        or counters["image_pixels"] > MAX_TOTAL_PIXELS
                    ):
                        raise JobError("Office images exceed their aggregate limit")
                elif "/fonts/" not in lower:
                    raise JobError("Unsupported Office binary package entry")
            required = {
                "docx": "word/document.xml",
                "xlsx": "xl/workbook.xml",
                "pptx": "ppt/presentation.xml",
            }[kind]
            if not {"[Content_Types].xml", "_rels/.rels", required} <= names:
                raise JobError("Office archive lacks required package metadata")
            return {"entries": len(entries), "uncompressed_bytes": total, **counters}
    except JobError:
        raise
    except Exception:
        raise JobError("Office ZIP package validation failed") from None


def _pdf_summary(module, data):
    if not data.startswith(b"%PDF-"):
        raise JobError("Converter returned an invalid PDF header")
    doc = native("converted PDF open", lambda: module.PdfDocument.from_bytes(data))
    count = native("converted page count", lambda: int(doc.page_count))
    if not 0 < count <= MAX_OFFICE_PAGES:
        raise JobError("Converted PDF exceeds the 250-page limit")
    native(
        "independent converted PDF structure validation",
        lambda: validate_pdf_bytes(data, expected_pages=count),
    )
    return count


def _pdf_dimensions(module, data):
    doc = native("output geometry open", lambda: module.PdfDocument.from_bytes(data))
    count = native("output geometry page count", lambda: int(doc.page_count))
    if not 0 < count <= MAX_OFFICE_PAGES:
        raise JobError("Converted PDF exceeds the geometry-check page limit")
    return [
        {"page": page + 1, "width": width, "height": height}
        for page in range(count)
        for width, height in [_page_extent(doc, page)]
    ]


def _office(request, options, add):
    module = require_runtime(request)
    partial = False
    warnings = [
        "Office conversion can change or omit layout, text, numbers, tables, images and fonts; "
        "compare the derivative with the original."
    ]
    if request["operation"] == "oxide_office_export":
        doc, _, source = open_source(request, options)
        count = source["pages"]
        if count > MAX_OFFICE_PAGES:
            raise JobError("Office export is limited to 250 source pages")
        kind = options["format"]
        vector_pages, path_count = [], 0
        if kind in {"xlsx", "pptx"}:
            for page in range(count):
                paths = native("Office vector inventory", lambda page=page: doc.extract_paths(page))
                path_count += len(paths)
                if path_count > 10_000:
                    raise JobError("Office vector inventory exceeds the 10,000-path limit")
                if paths:
                    vector_pages.append({"page": page + 1, "paths": len(paths)})
            if vector_pages:
                partial = True
                warnings.append(
                    "Partial conversion: pinned XLSX/PPTX export omits ordinary vector graphics "
                    "in reviewed fixtures, including table borders. "
                    "Source vector paths were detected."
                )
        method = getattr(doc, f"to_{kind}_bytes")
        data = native(f"{kind.upper()} export", method)
        if len(data) > MAX_ZIP_BYTES:
            raise JobError("Office output exceeds the 64-MiB limit")
        # Validate the generated archive too; an emitted OOXML file is not trusted.
        package = office_preflight(data, kind)
        threshold = 200 if kind == "xlsx" else 30
        report = {
            "source": source,
            "format": kind,
            "direction": "pdf_to_office",
            "upstream_layout_policy": "positioned" if count <= threshold else "flow",
            "layout_policy_threshold_pages": threshold,
            "layout_policy_basis": "pinned source branch; explicit flow API is not Python-bound",
            "package": package,
            "structured_warnings": _diagnostics(doc),
            "source_vector_pages": vector_pages,
            "known_graphics_gap": partial,
        }
        status = _diagnostic_status(report["structured_warnings"], range(count), needs_text=True)
        report.update(status)
        report["page_diagnostics"] = [
            {
                "page": page + 1,
                **_diagnostic_status(report["structured_warnings"], {page}, needs_text=True),
            }
            for page in range(count)
        ]
        partial = partial or status["partial"]
        warnings.extend(_diagnostic_warning(status))
        filename = f"converted.{kind}"
    else:
        if options.get("password"):
            raise JobError("Encrypted Office files are unsupported")
        data, source = _read_source(request)
        kind = options["format"]
        if kind == "auto":
            kind = Path(source["name"]).suffix.lower().lstrip(".")
        package = office_preflight(data, kind)
        method = getattr(module.OfficeConverter, f"from_{kind}_bytes")
        print("PDF Oxide: Office package validated; starting native import", flush=True)
        # office_oxide spawns a parsing thread with the default small Python
        # stack. The binding retains the GIL while joining it; its pyo3_log
        # callback can then deadlock requesting that GIL. This worker handles
        # one file and exits, so disabling native log callbacks stays local.
        native("Office log callback isolation", module.disable_logging)
        pdf = native(f"{kind.upper()} import", lambda: method(data))
        print("PDF Oxide: native Office import finished; validating PDF", flush=True)
        data = native("converted PDF serialization", pdf.to_bytes)
        count = _pdf_summary(module, data)
        report = {
            "source": source,
            "format": kind,
            "direction": "office_to_pdf",
            "package": package,
            "output_pages": count,
            "output_structure_validation": PDF_VALIDATION,
            "structured_warnings": [],
            "diagnostics_status": "OfficeConverter does not expose structured diagnostics",
            "native_log_status": (
                "disabled in disposable worker to avoid parser-thread/GIL deadlock"
            ),
        }
        filename = "converted.pdf"
    write_bytes(request, add, filename, data)
    report.update(
        {
            "engine": "pdf-oxide==0.3.78",
            "output": filename,
            "output_sha256": _hash(data),
            "fidelity_verified": False,
            "text_numbers_tables_verified": False,
            "partial": partial,
        }
    )
    write_json(request, add, "conversion.json", report)
    return warnings, partial


def _checked_visible_geometry(doc, source_bytes, options):
    """Compare native extents against independent physical page metadata.

    PyMuPDF is a read-only geometry validator here. PDF Oxide remains the only
    renderer and output writer; disagreement fails instead of changing engines.
    """
    import pymupdf

    result = []
    with pymupdf.open(stream=source_bytes, filetype="pdf") as independent:
        if independent.needs_pass and not independent.authenticate(options.get("password", "")):
            raise JobError("Independent source geometry authentication failed")
        if not 0 < len(independent) <= 100:
            raise JobError("Independent source geometry exceeds the page limit")
        for index, page in enumerate(independent):
            width, height = _page_extent(doc, index)
            if not (
                math.isclose(width, page.rect.width, abs_tol=0.01, rel_tol=0)
                and math.isclose(height, page.rect.height, abs_tol=0.01, rel_tol=0)
            ):
                raise JobError(
                    "Native and independent physical page dimensions disagree; "
                    "unsupported page units or box geometry, no output kept"
                )
            result.append(
                {
                    "page": index + 1,
                    "width": width,
                    "height": height,
                    "media_box": list(
                        native("source media box", lambda index=index: doc.page_media_box(index))
                    ),
                    "crop_box": native(
                        "source crop box", lambda index=index: doc.page_crop_box(index)
                    ),
                    "rotation_degrees": native(
                        "source rotation", lambda index=index: doc.page_rotation(index)
                    ),
                    "output_box": [0, 0, width, height],
                    "output_rotation_degrees": 0,
                }
            )
    return result


def _flatten_visible_page(request, options, module, doc, source, source_bytes, add):
    geometry = native(
        "independent source geometry validation",
        lambda: _checked_visible_geometry(doc, source_bytes, options),
    )
    if len(geometry) != source["pages"]:
        raise JobError("Independent and native page counts disagree; no output kept")
    builder = native("native raster PDF builder", module.DocumentBuilder)
    png_total, page_rasters = 0, []
    for entry in geometry:
        index, width, height = entry["page"] - 1, entry["width"], entry["height"]
        png = bytes(
            native(
                "visible-page rasterization",
                lambda index=index: doc.render_page(
                    index,
                    dpi=options["dpi"],
                    format="png",
                    background=(1, 1, 1, 1),
                    transparent=False,
                    render_annotations=True,
                    excluded_layers=[],
                ),
            )
        )
        png_total += len(png)
        if png_total > min(MAX_IMAGE_BYTES, request["limits"]["max_output_bytes"]):
            raise JobError("Rasterized page images exceed the aggregate byte limit")
        info = _encoded_image(png, allowed=("PNG",))
        expected = (math.ceil(width * options["dpi"] / 72), math.ceil(height * options["dpi"] / 72))
        if (info["width"], info["height"]) != expected:
            raise JobError("Renderer changed requested page resolution; no output kept")
        page = native(
            "native sized page", lambda width=width, height=height: builder.page(width, height)
        )
        native(
            "native full-page image placement",
            lambda page=page, png=png, width=width, height=height, index=index: page.image_with_alt(
                png, 0, 0, width, height, f"Rasterized source page {index + 1}"
            ),
        )
        native("native page completion", page.done)
        page_rasters.append(
            {
                "page": index + 1,
                "source_sha256": source["sha256"],
                "png_sha256": _hash(png),
                "png_bytes": len(png),
                "width_pixels": info["width"],
                "height_pixels": info["height"],
            }
        )
    data = native("native geometry-preserving PDF serialization", builder.build)
    count = _pdf_summary(module, data)
    dimensions = _pdf_dimensions(module, data)
    if count != source["pages"] or any(
        not math.isclose(actual[key], expected[key], abs_tol=0.01, rel_tol=0)
        for actual, expected in zip(dimensions, geometry, strict=True)
        for key in ("width", "height")
    ):
        raise JobError("Native output page geometry differs from source; no output kept")
    # Re-open independently too, including each final physical page rectangle.
    verified = native(
        "independent output geometry validation",
        lambda: _checked_visible_geometry(
            module.PdfDocument.from_bytes(data), data, {"password": ""}
        ),
    )
    if len(verified) != len(geometry):
        raise JobError("Independent output page count differs from source")
    records = _diagnostics(doc)
    status = _diagnostic_status(records, range(count))
    for entry in page_rasters:
        entry.update(_diagnostic_status(records, {entry["page"] - 1}))
    write_bytes(request, add, "rasterized.pdf", data)
    write_json(
        request,
        add,
        "conversion.json",
        {
            "engine": "pdf-oxide==0.3.78",
            "source": source,
            "operation": "render_page+DocumentBuilder",
            "layout": options["layout"],
            "native_methods": [
                "PdfDocument.render_page",
                "DocumentBuilder.page",
                "FluentPageBuilder.image_with_alt",
                "FluentPageBuilder.done",
                "DocumentBuilder.build",
            ],
            "renderer": "PDF Oxide",
            "container_writer": "PDF Oxide",
            "engine_fallback": False,
            "dpi": options["dpi"],
            "output": "rasterized.pdf",
            "output_sha256": _hash(data),
            "output_pages": count,
            "output_structure_validation": PDF_VALIDATION,
            "fidelity_verified": False,
            "redaction_verified": False,
            "source_page_dimensions": [
                {key: entry[key] for key in ("page", "width", "height")} for entry in geometry
            ],
            "source_geometry": geometry,
            "output_page_dimensions": dimensions,
            "physical_dimensions_checked": True,
            "geometry_validator": "PyMuPDF (metadata only; no rendering or output writing)",
            "page_rasters": page_rasters,
            **status,
            "layout_policy": (
                "Visible crop and rotation baked into a zero-origin page of the same visible "
                "physical size; original media/crop origins and rotation metadata are normalized"
            ),
            "native_flatten_limitation": (
                "PdfDocument.flatten_to_images still uses default paper and margins; "
                "this explicit native render/builder pipeline bypasses that helper"
            ),
            "lost_structure": ["text layer", "forms", "links", "signatures", "off-crop content"],
            "structured_warnings": records,
        },
    )
    return [
        "Visible physical page dimensions were checked; original page-box origins and rotation "
        "are normalized. Native rendering fidelity remains unverified. Rasterization removes "
        "text and interactive structure; this is not verified redaction.",
        *_diagnostic_warning(status),
    ], status["partial"]


def _flatten(request, options, add):
    module = require_runtime(request)
    doc, source_bytes, source = open_source(request, options)
    if source["pages"] > 100:
        raise JobError("Rasterization is limited to 100 source pages")
    total_pixels, source_dimensions = 0, []
    for page in range(source["pages"]):
        width, height = _page_extent(doc, page)
        source_dimensions.append({"page": page + 1, "width": width, "height": height})
        total_pixels += _pixel_size(
            math.ceil(width * options["dpi"] / 72), math.ceil(height * options["dpi"] / 72)
        )
        if total_pixels > MAX_TOTAL_PIXELS:
            raise JobError("Rasterization exceeds the 64-million-pixel document limit")
    if options["layout"] == "preserve_visible_page":
        return _flatten_visible_page(request, options, module, doc, source, source_bytes, add)
    # The native API writes temporary PNGs before building its returned PDF.
    # Force Rust's std::env::temp_dir into this disposable worker's directory;
    # the outer temporary directory also cleans native partial files on errors.
    with tempfile.TemporaryDirectory(prefix="oxide-flatten-", dir=request["workdir"]) as temporary:
        previous = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = temporary
        try:
            data = native("page image flattening", lambda: doc.flatten_to_images(options["dpi"]))
        finally:
            if previous is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = previous
    count = _pdf_summary(module, data)
    if count != source["pages"]:
        raise JobError("Rasterized PDF page count differs from source")
    records = _diagnostics(doc)
    status = _diagnostic_status(records, range(count))
    write_bytes(request, add, "rasterized.pdf", data)
    write_json(
        request,
        add,
        "conversion.json",
        {
            "engine": "pdf-oxide==0.3.78",
            "source": source,
            "operation": "flatten_to_images",
            "dpi": options["dpi"],
            "output": "rasterized.pdf",
            "output_sha256": _hash(data),
            "output_pages": count,
            "output_structure_validation": PDF_VALIDATION,
            "fidelity_verified": False,
            "redaction_verified": False,
            "source_page_dimensions": source_dimensions,
            "output_page_dimensions": _pdf_dimensions(module, data),
            **status,
            "partial": True,
            "layout_policy": "Native default paper and margins; source geometry is not retained",
            "lost_structure": ["text layer", "forms", "links", "signatures"],
            "structured_warnings": records,
        },
    )
    return [
        "Partial conversion: native rasterization changes paper and margins, and removes text "
        "and interactive structure. Source geometry and visual fidelity are not retained.",
        *_diagnostic_warning(status),
    ], True


def _image_pdf(request, options, add):
    module = require_runtime(request)
    if options.get("password"):
        raise JobError("Image passwords are unsupported")
    count = len(request["inputs"])
    if not 0 < count <= MAX_IMAGES or (request["operation"] == "oxide_image_pdf" and count != 1):
        raise JobError("Image conversion requires 1 to 100 bounded inputs")
    sources, total_pixels, total_bytes, snapshot_bytes = [], 0, 0, 0
    with tempfile.TemporaryDirectory(prefix="oxide-images-", dir=request["workdir"]) as temporary:
        paths = []
        for index in range(count):
            data, source = _read_source(request, index=index)
            info = _encoded_image(data)
            total_pixels += info["width"] * info["height"]
            total_bytes += len(data)
            if total_pixels > MAX_TOTAL_PIXELS or total_bytes > MAX_ZIP_BYTES:
                raise JobError("Image inputs exceed the aggregate pixel or byte limit")
            transformation = None
            if info["mode"] == "CMYK":
                if not options["normalize_cmyk"]:
                    raise JobError(
                        "Pinned PDF Oxide incorrectly preserves CMYK JPEG color; "
                        "enable explicit RGB normalization or use an RGB input"
                    )
                from PIL import Image

                with Image.open(io.BytesIO(data)) as image:
                    converted = image.convert("RGB")
                    stream = io.BytesIO()
                    converted.save(stream, format="PNG")
                data = stream.getvalue()
                _encoded_image(data)
                transformation = {
                    "operation": "CMYK to RGB using Pillow",
                    "profile_policy": "unprofiled conversion; print color accuracy unverified",
                    "native_input_format": "PNG",
                    "native_input_sha256": _hash(data),
                }
            sources.append(
                {
                    **source,
                    **info,
                    "output_page": index + 1,
                    "transformation": transformation,
                }
            )
            snapshot_bytes += len(data)
            if snapshot_bytes > MAX_ZIP_BYTES:
                raise JobError("Normalized image snapshots exceed the aggregate byte limit")
            if count == 1:
                pdf = native(
                    "image PDF creation", lambda data=data: module.Pdf.from_image_bytes(data)
                )
            else:
                image_format = "png" if transformation else info["format"].lower()
                path = Path(temporary) / f"image-{index:04d}.{image_format}"
                path.write_bytes(data)
                paths.append(str(path))
        if count != 1:
            pdf = native("multi-image PDF creation", lambda: module.Pdf.from_images(paths))
        data = native("image PDF serialization", pdf.to_bytes)
    pages = _pdf_summary(module, data)
    if pages != count:
        raise JobError("Image PDF page count does not match its input count")
    write_bytes(request, add, "images.pdf", data)
    write_json(
        request,
        add,
        "conversion.json",
        {
            "engine": "pdf-oxide==0.3.78",
            "operation": request["operation"],
            "sources": sources,
            "output": "images.pdf",
            "output_sha256": _hash(data),
            "output_pages": pages,
            "output_structure_validation": PDF_VALIDATION,
            "output_page_dimensions": _pdf_dimensions(module, data),
            "layout_policy": "Images centered and fitted on native default paper with margins",
            "fidelity_verified": False,
            "diagnostics_status": "Pdf image constructors do not expose structured diagnostics",
        },
    )
    return [
        "Review image color, transparency, orientation and dimensions in the derivative PDF."
    ], False


def process(request, options, add):
    validate_options(request["operation"], options)
    require_runtime(request)
    if request["operation"] == "oxide_flatten":
        return _flatten(request, options, add)
    if request["operation"] == "oxide_render":
        return _render(request, options, add)
    if request["operation"] in {"oxide_office_export", "oxide_office_import"}:
        return _office(request, options, add)
    if request["operation"] in {"oxide_image_pdf", "oxide_images_pdf"}:
        return _image_pdf(request, options, add)
    raise JobError("Unsupported PDF Oxide conversion operation")
