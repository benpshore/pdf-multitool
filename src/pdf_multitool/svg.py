"""Bounded, passive SVG page exports from the pinned MuPDF renderer.

This validates engine-generated output; it is not a general SVG upload sanitizer.
Document parsing and XML validation both run in the disposable document worker.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import re
import xml.etree.ElementTree as ET

from .core import JobError, parse_pages

SVG_NS = "http://www.w3.org/2000/svg"
MAX_SVG_BYTES = 16 * 1024 * 1024
MAX_SVG_NODES = 100_000
MAX_SVG_SIDE = 14_400
PASSIVE_TAGS = {
    "svg",
    "g",
    "defs",
    "path",
    "use",
    "image",
    "text",
    "tspan",
    "clipPath",
    "mask",
    "pattern",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "linearGradient",
    "radialGradient",
    "stop",
    "filter",
    "feBlend",
    "feColorMatrix",
    "feComposite",
    "feFlood",
    "feGaussianBlur",
    "feOffset",
    "feMerge",
    "feMergeNode",
    "title",
    "desc",
}
FRAGMENT = re.compile(r"#[A-Za-z_][\w.:-]*\Z", re.ASCII)
DATA_IMAGE = re.compile(r"data:image/(?:png|jpeg);base64,[A-Za-z0-9+/=\s]+\Z", re.ASCII)
PASSIVE_ELEMENTS = {f"{{{SVG_NS}}}{tag}" for tag in PASSIVE_TAGS}


def validate_svg(data: bytes, width: float, height: float) -> int:
    if len(data) > MAX_SVG_BYTES:
        raise JobError("SVG page exceeds the 16 MiB output limit")
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise JobError("SVG output contains a forbidden document type or entity")
    try:
        # DTDs/entities are rejected before parsing; data is bounded generated UTF-8.
        root = ET.fromstring(data)  # noqa: S314
    except ET.ParseError as error:
        raise JobError("Engine produced invalid SVG XML") from error
    if root.tag != f"{{{SVG_NS}}}svg":
        raise JobError("Engine did not produce an SVG document")
    try:
        box = [float(value) for value in root.attrib["viewBox"].split()]
        actual_width = float(root.attrib["width"].removesuffix("pt"))
        actual_height = float(root.attrib["height"].removesuffix("pt"))
    except (ValueError, KeyError) as error:
        raise JobError("SVG output has invalid page dimensions") from error
    expected = [0, 0, width, height, width, height]
    actual = [*box, actual_width, actual_height]
    if len(actual) != 6 or any(
        not math.isfinite(value) or abs(value - target) > 0.02
        for value, target in zip(actual, expected, strict=True)
    ):
        raise JobError("SVG page dimensions differ from the selected PDF page")
    embedded_pixels = 0
    for count, element in enumerate(root.iter(), 1):
        if count > MAX_SVG_NODES:
            raise JobError("SVG page exceeds the 100,000-element limit")
        if element.tag not in PASSIVE_ELEMENTS:
            raise JobError("SVG output contains an unsupported active or foreign element")
        for key, value in element.attrib.items():
            local = key.rsplit("}", 1)[-1]
            if (
                local.lower().startswith("on")
                or key == "{http://www.w3.org/XML/1998/namespace}base"
            ):
                raise JobError("SVG output contains an active attribute")
            if local in {"href", "src"} and not (
                FRAGMENT.fullmatch(value)
                or (element.tag == f"{{{SVG_NS}}}image" and DATA_IMAGE.fullmatch(value))
            ):
                raise JobError("SVG output references an external resource")
            if local in {"href", "src"} and value.startswith("data:"):
                from PIL import Image

                try:
                    encoded_image = base64.b64decode(
                        "".join(value.split(",", 1)[1].split()), validate=True
                    )
                    with Image.open(io.BytesIO(encoded_image)) as image:
                        embedded_pixels += image.width * image.height
                        if (
                            image.format not in {"PNG", "JPEG"}
                            or max(image.size) > 16_000
                            or embedded_pixels > 25_000_000
                        ):
                            raise JobError("SVG embedded images exceed their pixel or format limit")
                        image.verify()
                except (OSError, ValueError, binascii.Error, Image.DecompressionBombError) as error:
                    raise JobError("SVG output contains an invalid embedded image") from error
            if (
                local in {"fill", "stroke", "clip-path", "mask", "filter", "style"}
                and "url" in value.lower()
            ):
                # Accept only ordinary internal paint/clip references from this renderer.
                references = re.findall(r"url\(([^)]*)\)", value)
                remainder = re.sub(r"url\([^)]*\)", "", value)
                if "url" in remainder.lower() or any(
                    not FRAGMENT.fullmatch(item) for item in references
                ):
                    raise JobError("SVG output contains an unsupported resource reference")
            if local == "style" and ("\\" in value or "@" in value):
                raise JobError("SVG output contains unsupported CSS")
    return embedded_pixels


def export_pages(doc, options, workdir, limits, add):
    import pymupdf

    selected = parse_pages(options["pages"], len(doc))
    if len(selected) > 100:
        raise JobError("SVG export is limited to 100 selected pages")
    scale = options["scale"]
    matrix = pymupdf.Matrix(scale, scale)
    page_records, total_bytes, total_pixels = [], 0, 0
    for index in selected:
        page = doc[index]
        width, height = page.rect.width * scale, page.rect.height * scale
        if not all(math.isfinite(v) and 0 < v <= MAX_SVG_SIDE for v in (width, height)):
            raise JobError("Scaled SVG page exceeds the 14,400-point dimension limit")
        data = page.get_svg_image(matrix=matrix, text_as_path=options["text_mode"] == "paths")
        # Check before UTF-8 duplication; the byte check below remains authoritative.
        if len(data) > MAX_SVG_BYTES:
            raise JobError("SVG page exceeds the 16 MiB output limit")
        encoded = data.encode("utf-8")
        total_bytes += len(encoded)
        if total_bytes > limits["max_output_bytes"]:
            raise JobError("SVG export exceeds the configured output limit")
        total_pixels += validate_svg(encoded, width, height)
        if total_pixels > 200_000_000:
            raise JobError("SVG exports exceed the 200-million embedded-pixel limit")
        name = f"page-{index + 1:04d}.svg"
        path = workdir / name
        path.write_bytes(encoded)
        download_name = add(path)
        page_records.append(
            {
                "source_page": index + 1,
                "file": download_name,
                "width_points": width,
                "height_points": height,
                "rotation": page.rotation,
                "source_cropbox": list(page.cropbox),
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )
    warnings = [
        "SVG exports are page images. PDF links, interactive forms, attachments and "
        "cryptographic signatures are not transferred. Review every page."
    ]
    if options["text_mode"] == "text":
        warnings.append(
            "SVG text references fonts without embedding them. Viewers may substitute "
            "missing fonts; use paths for glyph-shape preservation."
        )
    report = {
        "schema_version": "svg-export/v1",
        "feature_id": "PY-006",
        "engine": "pymupdf",
        "version": pymupdf.__version__,
        "text_mode": options["text_mode"],
        "scale": scale,
        "pages": page_records,
        "warnings": warnings,
        "visual_review": "not_reviewed",
    }
    target = workdir / "svg-export.json"
    target.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    add(target)
    return warnings
