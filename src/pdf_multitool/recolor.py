"""Bounded whole-document color conversion in the disposable PDF worker."""

from __future__ import annotations

import hashlib
import json

from .core import JobError

COMPONENTS = {"gray": 1, "rgb": 3, "cmyk": 4}


def page_record(page):
    text = page.get_text()
    return {
        "page": page.number + 1,
        "rotation": page.rotation,
        "mediabox": list(page.mediabox),
        "cropbox": list(page.cropbox),
        "text_characters": len(text),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def convert_document(doc, options, workdir, limits, add):
    import pymupdf

    if not options["allow_color_change"]:
        raise JobError("Explicitly allow color conversion in the new PDF")
    if len(doc) > 100 or doc.xref_length() > 100_000:
        raise JobError("Color conversion is limited to 100 pages and 100,000 PDF objects")
    before, images, pixels, characters = [], set(), 0, 0
    visible_images, visible_pixels = 0, 0
    for page in doc:
        record = page_record(page)
        characters += record["text_characters"]
        if characters > 500_000:
            raise JobError("Color conversion exceeds the 500,000-character verification limit")
        before.append(record)
        # Cover visible inline images as well as ordinary image resources. Native
        # inspection itself remains under the worker's memory/CPU/time limits.
        for image in page.get_image_info():
            width, height = image["width"], image["height"]
            visible_images += 1
            visible_pixels += width * height
            if (
                visible_images > 1000
                or min(width, height) <= 0
                or max(width, height) > 16_000
                or width * height > 25_000_000
                or visible_pixels > 200_000_000
            ):
                raise JobError("Color conversion exceeds visible-image or decoded-pixel limits")
        # Include image resources even when they are not painted on a page.
        for entry in page.get_images(full=True):
            xref, _, width, height = entry[:4]
            if xref in images:
                continue
            images.add(xref)
            pixels += width * height
            if (
                len(images) > 256
                or min(width, height) <= 0
                or max(width, height) > 16_000
                or width * height > 25_000_000
                or pixels > 200_000_000
            ):
                raise JobError("Color conversion exceeds image-count or decoded-pixel limits")
    components = COMPONENTS[options["colorspace"]]
    # Document.recolor delegates to Page.recolor for each page in this pinned engine.
    # Only whole-document conversion is exposed; no cross-page resource scope promise.
    doc.recolor(components=components)
    output = workdir / "recolored.pdf"
    doc.save(output, garbage=3, deflate=True)
    if output.stat().st_size > limits["max_output_bytes"]:
        raise JobError("Recolored PDF exceeds the configured output limit")
    with pymupdf.open(output) as check:
        if len(check) != len(before) or [page_record(page) for page in check] != before:
            raise JobError(
                "Recolored PDF failed page geometry or extracted-text preservation checks"
            )
    name = add(output)
    warnings = [
        "Colors were converted on every page. Conversion can lose color information "
        "and invalidates cryptographic signatures. Review all pages; originals are retained.",
        "This uses the engine's color conversion and profiles, not calibrated print "
        "proofing. Spot colors, overprint, annotation/form appearance colors and "
        "archival/accessibility conformance are not qualified.",
    ]
    report = {
        "schema_version": "pdf-recolor/v1",
        "feature_id": "PY-036",
        "engine": "pymupdf",
        "version": pymupdf.__version__,
        "scope": "whole_document",
        "colorspace": options["colorspace"],
        "color_components": components,
        "pages": before,
        "image_resources_checked": len(images),
        "image_pixels_checked": pixels,
        "visible_image_instances_checked": visible_images,
        "visible_image_pixels_checked": visible_pixels,
        "output": {
            "file": name,
            "bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        },
        "verification": "Reopened PDF page geometry and extracted text match the source",
        "warnings": warnings,
        "visual_review": "not_reviewed",
    }
    target = workdir / "recolor.json"
    target.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    add(target)
    return warnings
