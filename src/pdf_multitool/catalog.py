"""Public contracts for additional engines, shared by API, CLI and browser controls."""

from __future__ import annotations

import math

READING_ORDERS = ("auto", "rows", "columns")
READING_ORDER_OPERATIONS = frozenset({"markdown", "textlayout", "pdfhtml"})


def validate_reading_order(value):
    if not isinstance(value, str) or value not in READING_ORDERS:
        raise ValueError("reading_order must be auto, rows or columns")
    return value


def field(label, kind="string", default="", **kwargs):
    return {"label": label, "type": kind, "default": default, **kwargs}


OPERATIONS = {
    "oxide": {
        "label": "Extract with PDF Oxide",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": True,
        "description": (
            "Extract native text and candidate table cells with optional PDF Oxide. "
            "Compare numbers, missing cells and wrapped rows with the original. "
            "No OCR. Optional header/footer cleanup keeps both text copies but can "
            "remove meaningful repeated prose, citations and table headers."
        ),
        "outputs": ["application/json", "text/plain", "text/html"],
        "options": {
            "mode": field("Extract", default="both", enum=["both", "text", "tables"]),
            "include_html": field("Additional page HTML copy", "boolean", False),
            "images": field(
                "Images (inspect keeps bytes in memory)",
                default="none",
                enum=["none", "inspect", "export"],
            ),
            "strategy": field("Table boundaries", default="both", enum=["both", "lines", "text"]),
            "cleanup": field(
                "Additional cleanup copy",
                default="none",
                enum=["none", "headers", "footers", "both"],
            ),
            "threshold": field(
                "Cleanup repetition fraction", "number", 0.8, minimum=0.5, maximum=1
            ),
        },
    },
    "recolor": {
        "label": "Convert PDF colors",
        "engine": "pymupdf",
        "inputs": 1,
        "pages": False,
        "feature_ids": ["PY-036"],
        "description": (
            "Convert the text, vectors and images on every page to gray, RGB or CMYK "
            "in a new PDF. Color information can be lost; changing back cannot restore "
            "it. Originals are retained. This is not a calibrated print-proofing tool."
        ),
        "outputs": ["application/pdf", "application/json"],
        "options": {
            "colorspace": field("Output color space", default="gray", enum=["gray", "rgb", "cmyk"]),
            "allow_color_change": field("Allow color conversion in the new PDF", "boolean", False),
        },
    },
    "svg": {
        "label": "Export SVG pages",
        "engine": "pymupdf",
        "inputs": 1,
        "pages": True,
        "feature_ids": ["PY-006"],
        "description": (
            "Export each selected PDF page as an SVG image, preserving its visible "
            "crop and rotation. Paths preserve glyph shapes; text mode needs matching "
            "fonts in the viewer. PDF links, forms and signatures are not transferred."
        ),
        "outputs": ["image/svg+xml", "application/json"],
        "options": {
            "text_mode": field("Text representation", default="paths", enum=["paths", "text"]),
            "scale": field("Page scale", "number", 1, minimum=0.25, maximum=4),
        },
    },
    "create": {
        "label": "Create text PDF",
        "engine": "pymupdf",
        "inputs": 0,
        "pages": False,
        "description": (
            "Create a paginated text PDF. Blank text makes one blank page. Uses a "
            "bundled Unicode font; missing glyphs fail explicitly."
        ),
        "outputs": ["application/pdf"],
        "options": {
            "content": field("Text", multiline=True, maxLength=20000),
            "paper": field("Paper", default="a4", enum=["a4", "letter"]),
            "font_size": field("Font size (points)", "number", 12, minimum=6, maximum=36),
            "margin": field("Margin (points)", "number", 36, minimum=18, maximum=144),
        },
    },
    "paginate": {
        "label": "Headers, footers & page numbers",
        "engine": "pymupdf",
        "inputs": 1,
        "pages": True,
        "description": (
            "Add a header and footer to chosen pages. Use {page} and {pages} for "
            "document page numbers. Existing content is preserved; review for "
            "overlap."
        ),
        "outputs": ["application/pdf"],
        "options": {
            "header": field("Header", maxLength=200),
            "footer": field("Footer", default="Page {page} of {pages}", maxLength=200),
            "font_size": field("Font size (points)", "number", 10, minimum=6, maximum=24),
            "margin": field("Margin (points)", "number", 24, minimum=12, maximum=100),
        },
    },
    "tables": {
        "label": "Extract tables",
        "engine": "pdfplumber",
        "inputs": 1,
        "pages": True,
        "description": (
            "Extract candidate tables as JSON and CSV with pdfplumber. Choose ruled "
            "lines or aligned text. Candidate cells require review; this does not "
            "repair general reading order."
        ),
        "outputs": ["application/json", "text/csv"],
        "options": {
            "strategy": field("Table boundaries", default="lines", enum=["lines", "text"]),
            "tolerance": field("Boundary tolerance (points)", "number", 3, minimum=0.1, maximum=10),
        },
    },
    "geometry": {
        "label": "Extract text & geometry",
        "engine": "pdfplumber",
        "inputs": 1,
        "pages": True,
        "description": (
            "Export text, words and character coordinates as JSON. Optional "
            "duplicate-character suppression affects this export only and is "
            "reported."
        ),
        "outputs": ["application/json", "text/plain"],
        "options": {
            "dedupe_chars": field("Suppress overlapping duplicate characters", "boolean", False),
            "tolerance": field("Duplicate tolerance (points)", "number", 1, minimum=0.1, maximum=5),
        },
    },
    "optimize": {
        "label": "Optimize PDF streams",
        "engine": "pikepdf",
        "inputs": 1,
        "pages": False,
        "description": (
            "Rewrite with pikepdf/qpdf using lossless Flate compression and object "
            "streams. No image downsampling. File size may increase; signatures "
            "become invalid."
        ),
        "outputs": ["application/pdf", "application/json"],
        "options": {
            "compression_level": field("Flate level", "integer", 6, minimum=1, maximum=9),
            "linearize": field("Optimize for sequential web viewing", "boolean", False),
        },
    },
    "inspectpdf": {
        "label": "Inspect structure & attachments",
        "engine": "pikepdf",
        "inputs": 1,
        "pages": False,
        "description": (
            "Report syntax warnings, metadata, outlines, attachments and media "
            "annotation types. Embedded media is not played and external links are "
            "not fetched."
        ),
        "outputs": ["application/json"],
        "options": {},
    },
    "attachments": {
        "label": "Extract embedded attachments",
        "engine": "pikepdf",
        "inputs": 1,
        "pages": False,
        "description": (
            "Save document-level embedded files under generated names, with a "
            "manifest of their original names and hashes. Files remain inert "
            "downloads; no codecs or programs run."
        ),
        "outputs": ["application/json", "application/octet-stream"],
        "options": {},
    },
    "ocr_tesseract": {
        "label": "OCR with Tesseract",
        "engine": "tesseract",
        "inputs": 1,
        "pages": True,
        "description": (
            "Rasterize selected PDF pages and recognize text with installed Tesseract "
            "language data. Produces a searchable PDF, text and word-confidence TSV. "
            "Review recognition and retain the original."
        ),
        "outputs": [
            "application/pdf",
            "text/plain",
            "text/tab-separated-values",
            "application/json",
        ],
        "options": {
            "language": field(
                "Installed language codes (for example eng)", default="eng", maxLength=80
            ),
            "dpi": field("OCR resolution (DPI)", "integer", 200, minimum=100, maximum=300),
            "psm": field("Page segmentation", "integer", 3, enum=[3, 6, 11]),
            "allow_rasterize": field(
                "Allow flattening into images (forms, links and signatures will not survive)",
                "boolean",
                False,
            ),
        },
    },
    "deskew": {
        "label": "Deskew scanned pages",
        "engine": "pillow",
        "inputs": 1,
        "pages": True,
        "description": (
            "Rasterize selected pages and correct a small skew using a bounded "
            "horizontal-line score, or an explicit angle. Reports every correction. "
            "Does not correct curved pages or perform OCR."
        ),
        "outputs": ["application/pdf", "application/json"],
        "options": {
            "mode": field("Angle selection", default="auto", enum=["auto", "manual"]),
            "angle": field(
                "Manual correction in degrees (counterclockwise)",
                "number",
                0,
                minimum=-10,
                maximum=10,
            ),
            "dpi": field("Resolution (DPI)", "integer", 144, minimum=72, maximum=300),
            "allow_rasterize": field(
                "Allow flattening into images (text layers, forms and links will not survive)",
                "boolean",
                False,
            ),
        },
    },
    "render_native": {
        "label": "Render with MuPDF or Ghostscript",
        "engine": "pymupdf",
        "inputs": 1,
        "pages": True,
        "description": (
            "Render selected pages to PNG using MuPDF or Ghostscript. Poppler "
            "rendering remains available in Render to images. Resolution and "
            "page-count limits apply."
        ),
        "outputs": ["image/png"],
        "options": {
            "renderer": field("Renderer", default="pymupdf", enum=["pymupdf", "ghostscript"]),
            "dpi": field("Resolution (DPI)", "integer", 144, minimum=36, maximum=300),
        },
    },
    "deduplicate": {
        "label": "Deduplicate PDF objects",
        "engine": "pymupdf",
        "inputs": 1,
        "pages": False,
        "description": (
            "Combine identical objects and stream bytes while retaining every page. "
            "Repeated page content is not deleted. Signatures become invalid and file "
            "size may increase."
        ),
        "outputs": ["application/pdf", "application/json"],
        "options": {},
    },
}


from .oxide_routes import contracts as oxide_contracts  # noqa: E402 -- after local definitions

OPERATIONS.update(oxide_contracts())

GAPS = [
    {
        "feature": "Curved-page dewarping",
        "status": "unsupported",
        "reason": (
            "No qualified curvature model or dewarp engine is integrated. Deskew only "
            "corrects planar rotation."
        ),
    },
    {
        "feature": "Docling OCR and table inference",
        "status": "unqualified",
        "reason": (
            "Requires an independently verified complete model manifest. Installing "
            "the extra does not qualify inference."
        ),
    },
    {
        "feature": "Docling audio/video transcription",
        "status": "unsupported",
        "reason": (
            "No qualified ASR model pipeline, bounded media decoder or media upload "
            "contract. Transcription is not codec muxing."
        ),
    },
    {
        "feature": "General audio/video codec conversion",
        "status": "unsupported",
        "reason": "Embedded attachment inspection/extraction only; no playback or codec muxing.",
    },
    {
        "feature": "Apple Vision / Neural Engine",
        "status": "unsupported",
        "reason": (
            "Requires separate Apple-platform qualification; this Linux workbench "
            "does not invoke it."
        ),
    },
    {
        "feature": "Poppler 26.10 source-built comparison",
        "status": "unqualified",
        "reason": (
            "System engine version is reported at runtime. No verified 26.10 build or "
            "comparison evidence is bundled."
        ),
    },
    {
        "feature": "Cryptographic signing and verification",
        "status": "unsupported",
        "reason": (
            "Signature fields are inspected only; no PKI validation, signing or "
            "credential handling."
        ),
    },
    {
        "feature": "PDF/A, PDF/UA and accessibility conformance",
        "status": "unqualified",
        "reason": (
            "No independent conformance validator is integrated. A successful rewrite "
            "is not a conformance claim."
        ),
    },
    {
        "feature": "Universal reading order and borderless-table semantics",
        "status": "partial",
        "reason": (
            "Frozen evidence preserves known ambiguous table/prose failures. Table "
            "candidates require review."
        ),
    },
    {
        "feature": "Native filesystem sandbox",
        "status": "unqualified",
        "reason": (
            "Process limits and network filters do not isolate native filesystem "
            "access. Host namespace support must be qualified separately."
        ),
    },
]


def validate_options(operation, options):
    from .core import JobError

    contract = OPERATIONS[operation]
    allowed = {**contract["options"]}
    if contract["inputs"]:
        allowed["password"] = field("Password", maxLength=127)
    if contract["pages"]:
        allowed["pages"] = field("Pages", maxLength=4096)
    unknown = options.keys() - allowed.keys()
    if unknown:
        raise JobError("Unknown option(s): " + ", ".join(sorted(unknown)))
    result = {}
    for key, spec in allowed.items():
        value = options.get(key, spec["default"])
        kind = spec["type"]
        valid = (
            type(value) is bool
            if kind == "boolean"
            else type(value) is int
            if kind == "integer"
            else type(value) in {int, float} and math.isfinite(value)
            if kind == "number"
            else isinstance(value, str)
        )
        if not valid:
            raise JobError(f"{key} must be {kind}")
        if "enum" in spec and value not in spec["enum"]:
            raise JobError(f"Unsupported {key}")
        if "minimum" in spec and not spec["minimum"] <= value <= spec["maximum"]:
            raise JobError(f"{key} must be between {spec['minimum']} and {spec['maximum']}")
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            raise JobError(f"{key} exceeds its length limit")
        result[key] = value
    if operation == "oxide":
        from .oxide import validate_selection

        validate_selection(result["pages"])
        if result["mode"] == "tables" and result["cleanup"] != "none":
            raise JobError("PDF Oxide cleanup requires text or both mode to retain both copies")
    elif operation.startswith("oxide_"):
        from .oxide import validate_selection
        from .oxide_routes import validate

        if contract["pages"]:
            validate_selection(result["pages"])
        validate(operation, result)
    return result


def capabilities(engine_status):
    return {
        "operations": {
            name: {
                **spec,
                "available": bool(engine_status.get(spec["engine"], {}).get("available"))
                and not bool(spec.get("blocked_reason")),
            }
            for name, spec in OPERATIONS.items()
        },
        "gaps": GAPS,
    }
