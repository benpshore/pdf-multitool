"""Shared bounds and inert output handling for optional PDF Oxide operations."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from . import oxide
from .core import JobError, parse_pages, safe_name


def require_runtime(request):
    if oxide._guard is None:
        raise JobError("PDF Oxide must run inside the bounded document worker")
    status = oxide.availability()
    if not status["available"]:
        raise JobError(status["reason"])
    import pdf_oxide

    return pdf_oxide


def native(stage, call):
    return oxide._native(stage, call)


def read_input(request, index=0):
    """Read an exact bounded regular-file snapshot, for PDF or non-PDF input."""
    require_runtime(request)
    maximum = min(request["limits"]["max_file_bytes"], oxide.MAX_SOURCE_BYTES)
    fd = os.open(request["inputs"][index], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise JobError("PDF Oxide requires a nonempty regular input within its byte limit")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise JobError("PDF Oxide input byte limit exceeded")
    return data


def open_source(request, options):
    module = require_runtime(request)
    data = read_input(request)
    if b"%PDF-" not in data[:1024]:
        raise JobError("PDF Oxide requires a PDF input")
    doc = native(
        "open/authentication",
        lambda: module.PdfDocument.from_bytes(data, options.get("password", "")),
    )
    count = native("page count", lambda: int(doc.page_count))
    if not 1 <= count <= min(request["limits"]["max_pages"], oxide.MAX_DOCUMENT_PAGES):
        raise JobError("PDF Oxide document page limit exceeded")
    return (
        doc,
        data,
        {
            "name": safe_name(request["names"][0]),
            "sha256": hashlib.sha256(data).hexdigest(),
            "pages": count,
        },
    )


def selected(options, count, maximum=100):
    pages = parse_pages(options.get("pages", ""), count)
    if len(pages) > min(maximum, oxide.MAX_PAGES):
        raise JobError("PDF Oxide selected page limit exceeded")
    return pages


def write_bytes(request, add, filename, data):
    require_runtime(request)
    if not isinstance(data, bytes | bytearray):
        raise JobError("PDF Oxide did not return binary output")
    if Path(filename).name != filename or safe_name(filename) != filename:
        raise JobError("PDF Oxide output name must be an app-owned safe basename")
    budget = request.setdefault("_oxide_output_budget", {"bytes": 0, "files": 0})
    budget["bytes"] += len(data)
    budget["files"] += 1
    if budget["bytes"] > min(request["limits"]["max_output_bytes"], oxide.MAX_OUTPUT_BYTES):
        raise JobError("PDF Oxide output byte limit exceeded; no output kept")
    if budget["files"] > min(request["limits"]["max_result_files"], 500):
        raise JobError("PDF Oxide output file-count limit exceeded")
    path = Path(request["workdir"]) / filename
    with path.open("xb") as stream:
        stream.write(data)
    add(path)
    return path


def write_json(request, add, filename, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8") + b"\n"
    return write_bytes(request, add, filename, data)


def validate_pdf_bytes(data, expected_pages=None, password=""):
    """Independent structural reopen; this does not establish content/visual fidelity."""
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass and not doc.authenticate(password):
            raise JobError("PDF Oxide output could not be authenticated independently")
        if not 1 <= len(doc) <= oxide.MAX_DOCUMENT_PAGES:
            raise JobError("PDF Oxide output has an unsupported page count")
        if expected_pages is not None and len(doc) != expected_pages:
            raise JobError("PDF Oxide output page count differs from requested result")
        for page in doc:
            if page.rect.is_empty or page.rect.is_infinite:
                raise JobError("PDF Oxide output contains an invalid page rectangle")
        return len(doc)
