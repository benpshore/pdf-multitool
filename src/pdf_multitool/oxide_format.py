"""Explicit page-wise native format conversion with untouched extraction baselines."""

from __future__ import annotations

import hashlib
import html

from .core import JobError


def _field(label, kind="string", default="", **kwargs):
    return {"label": label, "type": kind, "default": default, **kwargs}


OPERATIONS = {
    "oxide_format": {
        "label": "Export native text, Markdown or HTML",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": True,
        "description": (
            "Convert selected pages with native format controls; keep original native text "
            "separately. Reading order, headings and tables require source comparison. "
            "HTML includes an inert original and restricted review copy. No OCR or image export."
        ),
        "outputs": ["text/plain", "text/markdown", "text/html", "application/json"],
        "options": {
            "format": _field("Format", default="markdown", enum=["text", "markdown", "html"]),
            "preserve_layout": _field("Native layout preservation", "boolean", False),
            "detect_headings": _field("Native heading detection", "boolean", True),
            "include_artifacts": _field("Include tagged artifacts", "boolean", True),
            "include_form_fields": _field("Include fields (Markdown/HTML)", "boolean", True),
        },
    }
}


def validate_options(operation, options):
    if options["format"] == "html" and not options["include_artifacts"]:
        raise JobError("PDF Oxide HTML has no artifact exclusion control; choose text or Markdown")
    if options["format"] == "text" and not options["include_form_fields"]:
        raise JobError("PDF Oxide plain text has no form-field inclusion control")
    if options["format"] == "text" and not options["include_artifacts"]:
        raise JobError(
            "PDF Oxide 0.3.78 plain-text formatting ignores artifact exclusion; "
            "choose Markdown or the separate analysis text operation"
        )


def _review(source_hash, number, baseline, sanitized):
    return (
        '<!doctype html><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'\">"
        "<title>Native format review</title>"
        "<style>body{font:16px system-ui;margin:2rem}pre{white-space:pre-wrap}"
        "table{border-collapse:collapse}td,th{border:1px solid;padding:.4rem}</style>"
        f"<h1>Page {number}: native format candidates</h1>"
        "<p>Check text, numbers, tables and reading order against the original PDF. "
        "This is a filtered content review, not a reproduction of page appearance.</p>"
        f"<h2>Original native extraction</h2><pre>{html.escape(baseline)}</pre>"
        f'<h2>Filtered native HTML</h2><section id="source-{source_hash}-page-{number}">'
        f"{sanitized['fragment']}</section>"
    )


def _page_diagnostics(diagnostics, page):
    categories = {
        entry["native"]["category"] for entry in diagnostics if entry["page"] in {None, page}
    }
    truncated = "operator_cap_exceeded" in categories
    ocr_required = True if "no_text_layer" in categories else None
    omitted = sorted(categories & {"glyph_dropped", "image_suppressed"})
    return {
        "truncated": truncated,
        "ocr_required": ocr_required,
        "diagnostic_status": "warnings_present" if categories else "none_reported",
        "omitted_content_categories": omitted,
        "partial": truncated or ocr_required is True or bool(omitted),
    }


def process(request, options, add):
    from .oxide import MAX_CHARACTERS, VERSION, _warnings
    from .oxide_html import sanitize
    from .oxide_runtime import native, open_source, selected, write_bytes, write_json

    validate_options(request["operation"], options)
    doc, _, source = open_source(request, options)
    pages = selected(options, source["pages"])
    kind = options["format"]
    settings = {key: options[key] for key in OPERATIONS["oxide_format"]["options"]}
    common = {
        "preserve_layout": options["preserve_layout"],
        "detect_headings": options["detect_headings"],
        "include_images": False,
        "image_output_dir": None,
    }
    if kind != "html":
        common["include_artifacts"] = options["include_artifacts"]
    if kind != "text":
        common.update(embed_images=False, include_form_fields=options["include_form_fields"])
    methods = {"text": doc.to_plain_text, "markdown": doc.to_markdown, "html": doc.to_html}
    characters = 0
    page_records = []
    for index in pages:
        baseline = native(
            "format baseline extraction",
            lambda index=index: doc.extract_text(
                index, extract_tables=False, include_artifacts=True
            ),
        )
        value = native("page format conversion", lambda index=index: methods[kind](index, **common))
        if not isinstance(baseline, str) or not isinstance(value, str):
            raise JobError("PDF Oxide returned unsupported format output")
        characters += len(baseline) + len(value)
        if characters > MAX_CHARACTERS:
            raise JobError("PDF Oxide format character limit exceeded; no output kept")
        prefix = f"page-{index + 1:04d}"
        baseline_name = f"{prefix}.baseline.txt"
        write_bytes(request, add, baseline_name, baseline.encode())
        extension = {"text": "txt", "markdown": "md", "html": "native-html.txt"}[kind]
        filename = f"{prefix}.{extension}"
        data = value.encode()
        write_bytes(request, add, filename, data)
        record = {
            "page": index + 1,
            "native_page_index": index,
            "source_sha256": source["sha256"],
            "baseline": {
                "file": baseline_name,
                "sha256": hashlib.sha256(baseline.encode()).hexdigest(),
            },
            "native_output": {"file": filename, "sha256": hashlib.sha256(data).hexdigest()},
            "native_output_empty": not value.strip(),
            "semantics_verified": False,
        }
        if kind == "html":
            filtered = sanitize(value)
            review = _review(source["sha256"], index + 1, baseline, filtered)
            review_name = f"{prefix}.html"
            write_bytes(request, add, review_name, review.encode())
            record["review_html"] = {
                "file": review_name,
                **{key: val for key, val in filtered.items() if key != "fragment"},
            }
        page_records.append(record)
    diagnostics = _warnings(doc, source["sha256"], "format conversion")
    for record in page_records:
        record.update(_page_diagnostics(diagnostics, record["page"]))
    truncated = any(record["truncated"] for record in page_records)
    partial = any(record["partial"] for record in page_records)
    omitted = sorted(
        {category for record in page_records for category in record["omitted_content_categories"]}
    )
    write_json(
        request,
        add,
        "oxide-format-manifest.json",
        {
            "engine": "pdf-oxide",
            "version": VERSION,
            "source": source,
            "source_unchanged": True,
            "settings": settings,
            "applied_native_arguments": common,
            "pages": page_records,
            "structured_warnings": diagnostics,
            "images": "disabled; explicit bounded image export is a separate operation",
            "ocr": "not_run",
            "whole_document_method": "bounded explicit page loop; native *_all not called",
            "semantics_verified": False,
            "truncated": truncated,
            "partial": partial,
            "omitted_content_categories": omitted,
        },
    )
    warnings = [
        "Native formatting, reading order, headings and tables require comparison with the "
        "original PDF. Original native text is retained separately; "
        "process success is not correctness."
    ]
    if diagnostics:
        warnings.append("PDF Oxide reported structured diagnostics; inspect the format manifest.")
    if truncated:
        warnings.append(
            "Partial formatting: PDF Oxide reached its operator cap; native baseline and "
            "formatted output may omit source content."
        )
    if any(record["ocr_required"] is True for record in page_records):
        warnings.append("Partial formatting: source pages require OCR; OCR was not run.")
    if omitted:
        warnings.append(
            "Partial formatting: PDF Oxide reported omitted content (" + ", ".join(omitted) + ")."
        )
    return warnings, partial
