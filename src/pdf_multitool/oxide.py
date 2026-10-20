"""Optional, supervised PDF Oxide extraction; native candidates remain unverified."""

from __future__ import annotations

import base64
import hashlib
import html
import importlib.metadata
import json
import os
import stat
import struct
import sys
from pathlib import Path

from .core import JobError, parse_pages, safe_name

VERSION = "0.3.78"
MAX_PAGES = 100
MAX_DOCUMENT_PAGES = 2000
MAX_SOURCE_BYTES = 50 * 1024**2
MAX_OUTPUT_BYTES = 16 * 1024**2
MAX_CHARACTERS = 500_000
MAX_CELLS = 50_000
MAX_TABLES = 200
MAX_WARNINGS = 2000
MAX_IMAGES = 100
MAX_IMAGE_PIXELS = 25_000_000
MAX_TOTAL_IMAGE_PIXELS = 50_000_000
MAX_IMAGE_BYTES = 8 * 1024**2
MAX_TOTAL_IMAGE_BYTES = 16 * 1024**2
_guard = None


def availability():
    """Inspect distribution metadata without loading the native parser in the server."""
    try:
        version = importlib.metadata.version("pdf-oxide")
    except importlib.metadata.PackageNotFoundError:
        return {"available": False, "reason": "Install the optional oxide extra with uv"}
    supported = sys.platform == "linux" and os.getuid() != 0
    return {
        "available": supported and version == VERSION,
        "version": version,
        "reason": (
            "Worker checks required kernel bounds before parsing"
            if supported and version == VERSION
            else f"Requires pdf-oxide {VERSION} and nonroot Linux with libseccomp"
        ),
    }


def validate_selection(value):
    # Validate syntax and absolute bounds before spawning; no unbounded range expansion.
    # "all" depends on the actual source count, which only the bounded worker reads.
    if value.strip().lower() in {"", "all"}:
        return
    if len(parse_pages(value, MAX_DOCUMENT_PAGES)) > MAX_PAGES:
        raise JobError(f"PDF Oxide selects at most {MAX_PAGES} pages per input")


def initialize_guard(isolation, config):
    """Called only by worker.main after its resource limits and native filters are installed."""
    global _guard
    _guard = None
    if sys.platform != "linux" or not all(
        isolation.get(key) is True
        for key in (
            "native_network_filter",
            "process_group_confinement",
            "uid_process_limit_enforced",
        )
    ):
        raise JobError("PDF Oxide requires nonroot Linux native network/process confinement")
    import resource

    bounds = {}
    for name, ceiling in (
        ("RLIMIT_AS", min(config["memory_mb"], 2048) * 1024**2),
        ("RLIMIT_CPU", min(config["timeout_seconds"], 120)),
        ("RLIMIT_FSIZE", config["max_output_bytes"]),
        ("RLIMIT_NOFILE", 512),
        ("RLIMIT_NPROC", 4160),
    ):
        soft, hard = resource.getrlimit(getattr(resource, name))
        if not 0 < soft <= hard <= ceiling:
            raise JobError(f"PDF Oxide requires a finite enforced {name} bound")
        bounds[name] = {"soft": soft, "hard": hard}
    _guard = {"kernel_limits": bounds, **isolation}


def _native(stage, call):
    try:
        return call()
    except JobError:
        raise
    except Exception as exc:
        # Native exception text may contain document strings; never echo passwords/content.
        raise JobError(f"PDF Oxide {stage} failed ({type(exc).__name__}); no output kept") from None


def _source_bytes(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
            raise JobError("PDF Oxide requires a nonempty regular PDF within the input-size limit")
        data = stream.read(maximum + 1)
    if len(data) > maximum or b"%PDF-" not in data[:1024]:
        raise JobError("PDF Oxide input exceeds its size limit or lacks a PDF header")
    return data


class _Budget:
    def __init__(self, maximum):
        self.maximum = maximum
        self.bytes = self.characters = self.cells = self.tables = 0
        self.images = self.image_pixels = self.image_bytes = 0

    def content(self, text, tables):
        self.characters += len(text or "")
        self.tables += len(tables)
        for table in tables:
            if table["row_count"] != len(table["rows"]):
                raise JobError("PDF Oxide returned inconsistent table rows")
            for row in table["rows"]:
                self.cells += len(row["cells"])
                self.characters += sum(len(cell["text"]) for cell in row["cells"])
        if self.characters > MAX_CHARACTERS or self.cells > MAX_CELLS or self.tables > MAX_TABLES:
            raise JobError("PDF Oxide content limit exceeded; truncated output is not published")

    def write(self, path, content):
        data = content.encode("utf-8")
        self.bytes += len(data)
        if self.bytes > self.maximum:
            raise JobError("PDF Oxide output size limit exceeded; no output kept")
        path.write_bytes(data)

    def json(self, path, content):
        self.write(path, json.dumps(content, ensure_ascii=False, allow_nan=False, indent=2) + "\n")


def _warnings(doc, source_hash, phase):
    records = _native("structured diagnostics", doc.structured_warnings)
    if len(records) > MAX_WARNINGS or len(json.dumps(records)) > 256 * 1024:
        raise JobError("PDF Oxide diagnostic limit exceeded; no output kept")
    return [
        {
            "source_sha256": source_hash,
            "page": item["page"] + 1 if item.get("page") is not None else None,
            "region": None,  # This binding supplies no diagnostic region.
            "observed_after": phase,
            "native": item,
        }
        for item in records
    ]


def _images(doc, index, digest, budget, workdir, mode, add, review_images=None):
    # The metadata call itself is native parsing, already under hard AS/CPU limits.
    metadata = _native(f"page {index + 1} image metadata", lambda: doc.extract_images(index))
    budget.images += len(metadata)
    if budget.images > MAX_IMAGES:
        raise JobError("PDF Oxide image-count limit exceeded")
    for item in metadata:
        width, height = item["width"], item["height"]
        if not 0 < width * height <= MAX_IMAGE_PIXELS:
            raise JobError("PDF Oxide decoded image pixel limit exceeded")
        budget.image_pixels += width * height
    if budget.image_pixels > MAX_TOTAL_IMAGE_PIXELS:
        raise JobError("PDF Oxide total decoded image pixel limit exceeded")
    images = _native(f"page {index + 1} image bytes", lambda: doc.extract_image_bytes(index))
    if len(images) != len(metadata):
        raise JobError("PDF Oxide image metadata/byte counts differ")
    records = []
    for number, (item, info) in enumerate(zip(images, metadata, strict=True), 1):
        data = item["data"]
        budget.image_bytes += len(data)
        if len(data) > MAX_IMAGE_BYTES or budget.image_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise JobError("PDF Oxide image byte limit exceeded")
        if (
            item["format"] != "png"
            or data[:8] != b"\x89PNG\r\n\x1a\n"
            or len(data) < 24
            or data[12:16] != b"IHDR"
            or struct.unpack(">II", data[16:24]) != (info["width"], info["height"])
        ):
            raise JobError("PDF Oxide returned unsupported or inconsistent image bytes")
        record = {
            "source_sha256": digest,
            "page": index + 1,
            "image_index": number,
            "native_metadata": info,
            "format": "png",
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "persisted": mode == "export",
        }
        if mode == "export":
            out = workdir / f"page-{index + 1:04d}-image-{number:04d}.png"
            budget.bytes += len(data)
            if budget.bytes > budget.maximum:
                raise JobError("PDF Oxide output size limit exceeded; no output kept")
            out.write_bytes(data)
            add(out)
            record["file"] = out.name
            if review_images is not None:
                review_images.append(data)
        records.append(record)
    return records


def _review(page, image_previews=()):
    parts = [
        '<!doctype html><meta charset="utf-8">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; img-src data:\">",
        "<title>PDF Oxide extraction review</title>",
        "<style>body{font:16px system-ui;margin:2rem;max-width:90rem}"
        "table{border-collapse:collapse}td{border:1px solid;padding:.5rem;white-space:pre-wrap}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        ".image-preview{display:inline-block;max-width:100%;border:1px solid #555;"
        "background:repeating-conic-gradient(#fff 0% 25%,#ddd 0% 50%) 0/24px 24px}"
        ".image-preview img{display:block;max-width:100%;height:auto}"
        "figcaption{overflow-wrap:anywhere}</style>",
        f"<h1>Page {page['page']} · extraction candidates</h1>",
        "<p>Compare against the original PDF. Text, numbers, cell boundaries and reading order "
        "are unverified. This is a content review, not a reproduction of page layout.</p>",
        "<h2>Original native text</h2><pre>" + html.escape(page["text"] or "") + "</pre>",
    ]
    if page.get("partial"):
        parts.append(
            "<p><strong>Incomplete extraction:</strong> native diagnostics report omitted "
            "content, truncation or a missing text layer. Review the page JSON and source.</p>"
        )
    for index, table in enumerate(page["tables"], 1):
        parts.append(f"<h2>Candidate table {index}</h2><table>")
        for row in table["rows"]:
            parts.append("<tr>")
            parts.extend("<td>" + html.escape(cell["text"]) + "</td>" for cell in row["cells"])
            parts.append("</tr>")
        parts.append("</table>")
    if "images" in page:
        parts.extend(
            [
                "<h2>Native image extraction</h2>",
                "<p>These are native PNG conversions. Image completeness, color, masks and "
                "transparency are unverified. Compare against the original PDF; an empty "
                "result does not establish that the source has no images.</p>",
            ]
        )
        exported = [record for record in page["images"] if record["persisted"]]
        if len(exported) != len(image_previews):
            raise JobError("PDF Oxide image review does not match its explicit exports")
        for record, data in zip(exported, image_previews, strict=True):
            if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise JobError("PDF Oxide image review bytes differ from the recorded export")
            info = record["native_metadata"]
            number = record["image_index"]
            parts.extend(
                [
                    '<figure><div class="image-preview">',
                    f'<img alt="Native exported image {number}" src="data:image/png;base64,',
                    base64.b64encode(data).decode("ascii"),
                    '"></div><figcaption>',
                    f"Image {number}: {info['width']} x {info['height']} pixels. "
                    "Exact exported PNG bytes; checkerboard indicates transparent pixels. "
                    "SHA-256: " + html.escape(record["sha256"]),
                    "</figcaption></figure>",
                ]
            )
        if page["images"] and not exported:
            parts.append("<p>Inspect-only mode: image bytes were not persisted or embedded.</p>")
        elif not page["images"]:
            parts.append("<p>The native extractor returned no images.</p>")
    if "cleanup" in page:
        parts.extend(
            [
                "<h2>Optional cleanup copy · review removed content</h2>",
                "<p>Repeated prose, citations and table headers can be removed incorrectly. "
                "Original text and cells above are retained.</p><pre>",
                html.escape(page["cleanup"]["text"]),
                "</pre>",
            ]
        )
    if "html" in page:
        from .oxide_html import sanitize

        sanitized = sanitize(page["html"]["native"])
        parts.extend(
            [
                "<h2>Additional native HTML copy · filtered for review</h2>",
                "<p>Native reading order retained; styling, links and images removed. "
                "Native HTML table markup is not the direct-cell result above.</p>",
                f'<section id="source-{page["source_sha256"]}-page-{page["page"]}">',
                sanitized["fragment"],
                "</section>",
            ]
        )
    return "".join(parts)


def extract(request, options, add):
    if _guard is None:
        raise JobError("PDF Oxide must run inside the bounded document worker")
    status = availability()
    if not status["available"]:
        raise JobError(status["reason"])
    limits = request["limits"]
    workdir = Path(request["workdir"])
    data = _source_bytes(request["inputs"][0], min(limits["max_file_bytes"], MAX_SOURCE_BYTES))
    digest = hashlib.sha256(data).hexdigest()
    from pdf_oxide import PdfDocument

    # Always authenticate, including the empty password, rather than parsing encrypted gibberish.
    doc = _native("open/authentication", lambda: PdfDocument.from_bytes(data, options["password"]))
    count = _native("page count", lambda: int(doc.page_count))
    if not 1 <= count <= min(limits["max_pages"], MAX_DOCUMENT_PAGES):
        raise JobError("PDF Oxide page count is outside the supported limit")
    selected = parse_pages(options["pages"], count)
    if len(selected) > MAX_PAGES or (options["cleanup"] != "none" and count > MAX_PAGES):
        raise JobError("PDF Oxide limits selection and whole-document cleanup to 100 pages")
    budget = _Budget(min(limits["max_output_bytes"], MAX_OUTPUT_BYTES))
    report = {
        "schema_version": 1,
        "engine": {"name": "pdf-oxide", "version": VERSION},
        "source": {"name": safe_name(request["names"][0]), "sha256": digest, "pages": count},
        "settings": {
            key: options[key]
            for key in ("mode", "strategy", "cleanup", "threshold", "include_html", "images")
        },
        "bounds": _guard,
        "semantics_verified": False,
        "ocr": "not_run",
        "limits": {
            "selected_pages": MAX_PAGES,
            "source_bytes": min(limits["max_file_bytes"], MAX_SOURCE_BYTES),
            "cumulative_serialized_bytes": budget.maximum,
            "characters": MAX_CHARACTERS,
            "tables": MAX_TABLES,
            "cells": MAX_CELLS,
            "images": MAX_IMAGES,
            "image_pixels": MAX_IMAGE_PIXELS,
            "total_image_pixels": MAX_TOTAL_IMAGE_PIXELS,
            "image_bytes": MAX_IMAGE_BYTES,
            "total_image_bytes": MAX_TOTAL_IMAGE_BYTES,
        },
        "coordinate_convention": "Native [x,y,width,height]; crop/rotation mapping unqualified",
        "pages": [],
    }
    warnings = [
        "PDF Oxide text and table candidates require comparison with the original, including "
        "missing text, numbers, empty cells and wrapped rows. Process success is not correctness."
    ]
    partial = False
    image_previews = {}
    if options["images"] != "none":
        warnings.append(
            "Images are native PNG conversions, not guaranteed original compressed bytes. "
            "An empty image result does not prove the page is image-free; unsupported or "
            "malformed streams may be omitted. Compare masks, transparency and color with the PDF."
        )
    for index in selected:
        number = index + 1
        text = (
            _native(
                f"page {number} text",
                lambda i=index: doc.extract_text(i, extract_tables=False, include_artifacts=True),
            )
            if options["mode"] != "tables"
            else None
        )
        tables = (
            _native(
                f"page {number} tables",
                lambda i=index: doc.extract_tables(
                    i,
                    table_settings={
                        "horizontal_strategy": options["strategy"],
                        "vertical_strategy": options["strategy"],
                    },
                ),
            )
            if options["mode"] != "text"
            else []
        )
        budget.content(text, tables)
        page = {
            "source_sha256": digest,
            "page": number,
            "native_page_index": index,
            "text": text,
            "text_status": "not_requested"
            if text is None
            else "empty"
            if not text
            else "extracted",
            "tables": tables,  # Actual native rows/cells, with no reconstruction or padding.
            "table_status": (
                "not_requested"
                if options["mode"] == "text"
                else "candidates"
                if tables
                else "none_detected"
            ),
            "ocr": "not_run",
            "ocr_required": None,  # Empty text alone cannot distinguish a scan from a blank page.
            "truncated": False,
        }
        if options["include_html"]:
            from .oxide_html import sanitize

            native_html = _native(
                f"page {number} HTML",
                lambda i=index: doc.to_html(
                    i,
                    preserve_layout=False,
                    detect_headings=True,
                    include_images=False,
                    image_output_dir=None,
                    embed_images=False,
                    include_form_fields=True,
                ),
            )
            budget.content(native_html, [])
            transformed = sanitize(native_html)
            page["html"] = {
                "native": native_html,
                "images": "disabled",
                "status": "extracted" if native_html else "empty",
                "reading_order": "native_unverified",
                "sanitization": {
                    key: value for key, value in transformed.items() if key != "fragment"
                },
            }
            out = workdir / f"page-{number:04d}.native-html.txt"
            budget.write(out, native_html)
            add(out)
        if options["images"] != "none":
            image_previews[number] = []
            page["images"] = _images(
                doc, index, digest, budget, workdir, options["images"], add, image_previews[number]
            )
            page["image_status"] = "extracted" if page["images"] else "none_returned"
            page["image_completeness_verified"] = False
            page["image_fidelity_verified"] = False
            page["image_review"] = (
                "exact_exported_png_previews" if options["images"] == "export" else "metadata_only"
            )
        budget.json(workdir / f"page-{number:04d}.json", page)
        if text is not None:
            budget.write(workdir / f"page-{number:04d}.txt", text)
            add(workdir / f"page-{number:04d}.txt")
        report["pages"].append({"page": number, "file": f"page-{number:04d}.json"})
    report["structured_warnings"] = _warnings(doc, digest, "original_extraction")
    if options["cleanup"] != "none":
        # Cleanup mutates erase masks and caches. A fresh document cannot contaminate
        # original text/cells, subsequent jobs, or the already captured diagnostics.
        del doc
        doc = _native(
            "cleanup open/authentication", lambda: PdfDocument.from_bytes(data, options["password"])
        )
        method = {
            "headers": doc.remove_headers,
            "footers": doc.remove_footers,
            "both": doc.remove_artifacts,
        }
        removed = _native("cleanup", lambda: method[options["cleanup"]](options["threshold"]))
        report["cleanup"] = {
            "method": options["cleanup"],
            "threshold": options["threshold"],
            "native_removed_count": removed,
            "scope": "whole_document",
            "source_modified": False,
        }
        warnings.append(
            "Optional header/footer cleanup can remove meaningful content; review both copies."
        )
        for index in selected:
            path = workdir / f"page-{index + 1:04d}.json"
            page = json.loads(path.read_text(encoding="utf-8"))
            cleaned = _native(
                f"page {index + 1} cleanup text",
                lambda i=index: doc.extract_text(i, extract_tables=False, include_artifacts=True),
            )
            budget.content(cleaned, [])
            page["cleanup"] = {
                "text": cleaned,
                "changed": cleaned != page["text"],
                "tables_transformed": False,
            }
            budget.json(path, page)
            out = workdir / f"page-{index + 1:04d}-cleanup.txt"
            budget.write(out, cleaned)
            add(out)
        # This buffer belongs to the separate cleanup document.
        report["cleanup_structured_warnings"] = _warnings(doc, digest, "cleanup")
    data = b""
    for reference in report["pages"]:
        path = workdir / reference["file"]
        page = json.loads(path.read_text(encoding="utf-8"))
        applicable = [
            entry["native"]["category"]
            for entry in report["structured_warnings"]
            if entry["page"] in {None, page["page"]}
        ]
        page["ocr_required"] = True if "no_text_layer" in applicable else None
        page["truncated"] = "operator_cap_exceeded" in applicable
        page["omitted_content_categories"] = sorted(
            set(applicable) & {"glyph_dropped", "image_suppressed"}
        )
        page["diagnostic_status"] = "warnings_present" if applicable else "none_reported"
        page["partial"] = bool(
            page["truncated"] or page["ocr_required"] or page["omitted_content_categories"]
        )
        if "cleanup" in page:
            cleanup_categories = {
                entry["native"]["category"]
                for entry in report.get("cleanup_structured_warnings", [])
                if entry["page"] in {None, page["page"]}
            }
            page["cleanup"].update(
                truncated="operator_cap_exceeded" in cleanup_categories,
                ocr_required=True if "no_text_layer" in cleanup_categories else None,
                omitted_content_categories=sorted(
                    cleanup_categories & {"glyph_dropped", "image_suppressed"}
                ),
                partial=bool(
                    cleanup_categories
                    & {
                        "operator_cap_exceeded",
                        "no_text_layer",
                        "glyph_dropped",
                        "image_suppressed",
                    }
                ),
            )
            page["partial"] = page["partial"] or page["cleanup"]["partial"]
        partial = partial or page["partial"]
        budget.json(path, page)
        add(path)
        out = path.with_suffix(".html")
        budget.write(out, _review(page, image_previews.pop(page["page"], ())))
        add(out)
        reference.update(
            {
                key: page[key]
                for key in (
                    "text_status",
                    "table_status",
                    "ocr_required",
                    "truncated",
                    "partial",
                    "omitted_content_categories",
                    "diagnostic_status",
                )
            }
        )
        if "cleanup" in page:
            reference["cleanup_truncated"] = page["cleanup"]["truncated"]
    report["partial"] = partial
    report["truncated"] = any(
        entry["truncated"] or entry.get("cleanup_truncated") for entry in report["pages"]
    )
    if report["structured_warnings"] or report.get("cleanup_structured_warnings"):
        warnings.append(
            "PDF Oxide reported structured diagnostics; inspect the extraction manifest."
        )
    budget.json(workdir / "oxide-manifest.json", report)
    add(workdir / "oxide-manifest.json")
    return warnings, partial
