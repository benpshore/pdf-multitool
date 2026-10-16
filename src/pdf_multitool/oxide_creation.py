"""Bounded declarative creation with the pinned PDF Oxide Python builders.

The declaration is data, never a list of arbitrary Python calls. Assets are embedded
bytes, not paths. The Python streaming-table binding buffers all rows, so its total
input is capped just like a normal table. Native entry points run only in a guarded
worker. Successful serialization does not certify visual fidelity or PDF/UA conformance.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import io
import math
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .core import JobError, strict_json_loads

MAX_SPEC_BYTES = 200_000
MAX_CONTENT = 40_000
MAX_PAGES = 40
MAX_ELEMENTS = 1000
MAX_ROWS = 1000
MAX_CELLS = 10_000
MAX_ASSETS = 16
MAX_ASSET_BYTES = 120_000
MAX_IMAGE_PIXELS = 4_000_000
MAX_TOTAL_PIXELS = 8_000_000
BASE_FONTS = (
    "Helvetica",
    "Helvetica-Bold",
    "Helvetica-Oblique",
    "Helvetica-BoldOblique",
    "Times-Roman",
    "Times-Bold",
    "Times-Italic",
    "Times-BoldItalic",
    "Courier",
    "Courier-Bold",
    "Courier-Oblique",
    "Courier-BoldOblique",
)
BARCODES = {
    "code128": 0,
    "code39": 1,
    "ean13": 2,
    "ean8": 3,
    "upca": 4,
    "itf": 5,
    "code93": 6,
    "codabar": 7,
}


def _field(label, kind="string", default="", **kwargs):
    return {"label": label, "type": kind, "default": default, **kwargs}


OPERATIONS = {
    "oxide_create_text": {
        "label": "Create PDF with PDF Oxide",
        "engine": "pdf_oxide",
        "inputs": 0,
        "pages": False,
        "description": (
            "Create a PDF from text, Markdown or resource-free HTML. CSS mode requires "
            "embedded font bytes. Original content is retained in the request; outputs "
            "include its hash. Review all text, layout and glyphs."
        ),
        "outputs": ["application/pdf", "application/json"],
        "options": {
            "format": _field(
                "Source format", default="text", enum=["text", "markdown", "html", "html_css"]
            ),
            "content": _field("Content", multiline=True, maxLength=MAX_CONTENT),
            "title": _field("Title", maxLength=500),
            "author": _field("Author", maxLength=500),
            "header": _field("Markdown header", maxLength=500),
            "footer": _field("Markdown footer", maxLength=500),
            "header_left": _field("Markdown left header", maxLength=500),
            "footer_left": _field("Markdown left footer", maxLength=500),
            "css": _field("CSS (no resource URLs)", multiline=True, maxLength=20_000),
            "fonts": _field(
                "Font assets JSON (name, base64 data)",
                default="[]",
                multiline=True,
                maxLength=160_000,
            ),
        },
    },
    "oxide_create_document": {
        "label": "Build pages with PDF Oxide",
        "engine": "pdf_oxide",
        "inputs": 0,
        "pages": False,
        "description": (
            "Build bounded pages, text, graphics, supported forms and barcodes from a "
            "strict JSON declaration. Images/fonts use embedded bytes; no paths, "
            "scripts or external resources. Native tables and broken annotation "
            "types are blocked. Tagged output needs accessibility review."
        ),
        "outputs": ["application/pdf", "application/json"],
        "options": {
            "document": _field(
                "Document JSON",
                multiline=True,
                maxLength=MAX_SPEC_BYTES,
                default='{"pages":[{"size":"a4","elements":[{"type":"text",'
                '"text":"Hello","x":72,"y":740}]}]}',
            ),
        },
    },
    "oxide_create_barcode": {
        "label": "Create barcode SVG with PDF Oxide",
        "engine": "pdf_oxide",
        "inputs": 0,
        "pages": False,
        "description": (
            "Generate a bounded inert barcode SVG. Verify the decoded payload before use."
        ),
        "outputs": ["image/svg+xml", "application/json"],
        "options": {
            "symbology": _field("Symbology", default="qr", enum=[*BARCODES, "qr"]),
            "data": _field("Barcode data", maxLength=1024),
            "error_correction": _field(
                "QR error correction", default="medium", enum=["low", "medium", "quartile", "high"]
            ),
            "size": _field("QR size (pixels)", "integer", 256, minimum=21, maximum=1024),
        },
    },
    "oxide_create_pattern": {
        "label": "Export PDF Oxide pattern cell",
        "engine": "pdf_oxide",
        "inputs": 0,
        "pages": False,
        "description": (
            "Export a generated PDF pattern content fragment as text. This is a "
            "building block, not a complete PDF or a rendered page pattern."
        ),
        "outputs": ["text/plain", "application/json"],
        "options": {
            "preset": _field(
                "Pattern",
                default="checkerboard",
                enum=[
                    "horizontal_stripes",
                    "vertical_stripes",
                    "checkerboard",
                    "dots",
                    "diagonal_lines",
                    "crosshatch",
                ],
            ),
            "size": _field("Pattern size", "number", 24, minimum=2, maximum=256),
            "detail": _field(
                "Stripe width / dot radius / line width", "number", 2, minimum=0.25, maximum=128
            ),
            "color": _field("Primary RGB hex", default="#2255aa", maxLength=7),
            "secondary": _field("Checkerboard secondary RGB hex", default="#eeeeee", maxLength=7),
        },
    },
}


def _object(value, keys, label):
    if not isinstance(value, dict) or value.keys() - set(keys):
        raise JobError(f"Invalid {label} object or unsupported fields")
    return value


def _list(value, maximum, label, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise JobError(f"{label} requires {minimum} to {maximum} items")
    return value


def _text(value, maximum=2000, label="text"):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(char) < 32 and char not in "\n\r\t" for char in value)
    ):
        raise JobError(f"Invalid {label} or text limit exceeded")
    return value


def _number(value, low=-2000, high=2000, label="coordinate"):
    if type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high:
        raise JobError(f"{label} must be finite and between {low} and {high}")
    return value


def _integer(value, low, high, label):
    if type(value) is not int:
        raise JobError(f"{label} must be an integer")
    return _number(value, low, high, label)


def _boolean(value, label):
    if type(value) is not bool:
        raise JobError(f"{label} must be boolean")
    return value


def _choice(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise JobError(f"Unsupported {label}")
    return value


def _color(value):
    return tuple(_number(c, 0, 1, "color channel") for c in _list(value, 3, "color", 3))


def _rect(item):
    return (
        _number(item.get("x", 72)),
        _number(item.get("y", 600)),
        _number(item.get("width", 100), 1, 2000, "width"),
        _number(item.get("height", 30), 1, 2000, "height"),
    )


def _url(value):
    value = _text(value, 2000, "link URL")
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https", "mailto"} and (
            bool(parsed.netloc) if parsed.scheme != "mailto" else bool(parsed.path)
        )
    except ValueError:
        valid = False
    if not valid or any(c.isspace() for c in value):
        raise JobError("Links require an explicit HTTP, HTTPS or mailto URL")
    return value


def _assets(value, kind):
    result, total = {}, 0
    for item in _list(value, MAX_ASSETS, kind):
        _object(item, ("name", "data"), kind)
        name = _text(item.get("name"), 80, "asset name")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", name) or name in result:
            raise JobError("Asset names must be unique simple identifiers")
        if kind == "fonts" and name in BASE_FONTS:
            raise JobError("Embedded font names must not shadow built-in fonts")
        encoded = _text(item.get("data"), 160_000, "base64 asset")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError, binascii.Error:
            raise JobError("Asset must contain valid base64 bytes") from None
        total += len(data)
        if not data or total > MAX_ASSET_BYTES:
            raise JobError("Embedded asset byte limit exceeded")
        result[name] = data
    return result


class _Markup(HTMLParser):
    """Reject active/resource constructs; never silently drop input content."""

    TAGS = frozenset(
        {
            "html",
            "head",
            "title",
            "body",
            "div",
            "span",
            "p",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "b",
            "strong",
            "i",
            "em",
            "u",
            "s",
            "del",
            "br",
            "hr",
            "ul",
            "ol",
            "li",
            "table",
            "thead",
            "tbody",
            "tfoot",
            "tr",
            "th",
            "td",
            "caption",
            "pre",
            "code",
            "blockquote",
            "a",
            "section",
            "article",
            "header",
            "footer",
            "sub",
            "sup",
        }
    )

    def handle_starttag(self, tag, attrs):
        if tag not in self.TAGS:
            raise JobError("HTML contains an unsupported active or resource element")
        if len(attrs) != len({key for key, _ in attrs}):
            raise JobError("Duplicate HTML attributes are not supported")
        for key, value in attrs:
            if key not in {"class", "id", "title", "style", "href", "colspan", "rowspan"}:
                raise JobError("HTML attribute is not supported")
            _text(value, 2000, "HTML attribute")
            if key == "style":
                _css(value)
            elif key == "href":
                if tag != "a":
                    raise JobError("Only anchor elements may have href")
                _url(value)
            elif key in {"colspan", "rowspan"} and (
                not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 32
            ):
                raise JobError("Invalid HTML table span")

    def handle_endtag(self, tag):
        if tag not in self.TAGS:
            raise JobError("HTML contains an unsupported element")

    def handle_decl(self, decl):
        if decl.lower() != "doctype html":
            raise JobError("Only the HTML doctype is supported")

    def handle_pi(self, data):
        raise JobError("HTML processing instructions are unsupported")


def _css(value):
    _text(value, 20_000, "CSS")
    # No escapes/comments which can disguise a resource keyword, and no at-rules
    # (imports, font-face and external stylesheet mechanisms are unavailable).
    lower = value.lower()
    if any(token in lower for token in ("\\", "/*", "@", "url", "expression", "-moz-binding")):
        raise JobError("CSS resource loading, escapes, comments and at-rules are unsupported")


def _markup_content(content, kind):
    if kind in {"html", "html_css"}:
        parser = _Markup(convert_charrefs=True)
        parser.feed(content)
        parser.close()
        if re.search(r"<\s*(?:table|tr|td|th|thead|tbody|tfoot)\b", content, re.IGNORECASE):
            raise JobError(
                "PDF Oxide 0.3.78 HTML table creation failed visual layout checks; "
                "table output is blocked pending an upstream fix"
            )
    elif kind == "markdown":
        # Markdown's image/raw-HTML paths are not a confined asset interface.
        # Images are available in the explicit document declaration instead.
        if (
            "![" in content
            or "<" in content
            or re.search(r"\]\s*\(", content)
            or re.search(r"^\s*\[[^\]]+\]:", content, re.MULTILINE)
        ):
            raise JobError("Markdown images, links and raw HTML require the document builder")


def _columns(value, page_width):
    columns = _list(value, 32, "columns", 1)
    for column in columns:
        _object(column, ("header", "width", "align"), "table column")
        _text(column.get("header", ""))
        _number(column.get("width", 100), 10, 1000, "column width")
        _choice(column.get("align", "left"), ("left", "center", "right"), "alignment")
    if sum(c.get("width", 100) for c in columns) > page_width - 144:
        raise JobError("Table column widths exceed the page's bounded content width")
    return columns


def _table(item, page_width, budget):
    columns = _columns(item.get("columns"), page_width)
    rows = _list(item.get("rows"), MAX_ROWS, "table rows")
    budget["cells"] += len(rows) * len(columns)
    if budget["cells"] > MAX_CELLS:
        raise JobError("Creation table cell limit exceeded")
    for row in rows:
        _list(row, len(columns), "table row", len(columns))
        for cell in row:
            if isinstance(cell, dict) and item["type"] == "streaming_table":
                _object(cell, ("text", "rowspan"), "spanning cell")
                _integer(cell.get("rowspan", 1), 1, 32, "rowspan")
                cell = cell.get("text")
            budget["text"] += len(_text(cell))
    _boolean(item.get("header", False), "header")
    if item["type"] == "streaming_table":
        _choice(item.get("mode", "fixed"), ("fixed", "sample", "auto_all"), "table sizing")
        if item.get("mode", "fixed") == "auto_all":
            raise JobError(
                "PDF Oxide 0.3.78 auto_all tables dropped every tested cell; "
                "use fixed or sample sizing until that upstream failure is resolved"
            )
        _boolean(item.get("repeat_header", False), "repeat_header")
        _integer(item.get("sample_rows", 50), 1, 1000, "sample_rows")
        _integer(item.get("batch_size", 128), 1, 256, "batch_size")
        minimum = _number(item.get("min_col_width", 20), 10, 400, "minimum column width")
        _number(item.get("max_col_width", 400), minimum, 1000, "maximum column width")


TEXT_TYPES = frozenset(
    {
        "text",
        "heading",
        "paragraph",
        "inline",
        "inline_bold",
        "inline_italic",
        "inline_color",
        "columns",
        "footnote",
        "text_in_rect",
    }
)
FORM_TYPES = frozenset(
    {"text_field", "checkbox", "combo_box", "radio_group", "push_button", "signature_field"}
)
SHAPES = frozenset(
    {
        "rect",
        "filled_rect",
        "line",
        "stroke_rect",
        "stroke_rect_dashed",
        "stroke_line",
        "stroke_line_dashed",
    }
)
ANNOTATIONS = frozenset(
    {
        "link_url",
        "link_page",
        "link_named",
        "highlight",
        "underline",
        "strikeout",
        "squiggly",
        "sticky_note",
        "sticky_note_at",
        "watermark",
        "watermark_confidential",
        "watermark_draft",
        "stamp",
        "freetext",
    }
)
EMPTY_TYPES = frozenset({"horizontal_rule", "newline", "new_page"})

# Each element has its own data contract. A recognized but irrelevant option
# must not be accepted and silently ignored (for example color on plain text).
ELEMENT_FIELDS = {
    **{kind: {"text"} for kind in TEXT_TYPES},
    "heading": {"text", "level"},
    "inline_color": {"text", "color"},
    "columns": {"text", "count", "gap"},
    "footnote": {"text", "reference"},
    "text_in_rect": {"text", "width", "height", "align"},
    "rect": {"width", "height"},
    "filled_rect": {"width", "height", "color"},
    "line": {"x2", "y2"},
    "stroke_rect": {"width", "height", "stroke", "color"},
    "stroke_rect_dashed": {"width", "height", "stroke", "color", "dash", "phase"},
    "stroke_line": {"x2", "y2", "stroke", "color"},
    "stroke_line_dashed": {"x2", "y2", "stroke", "color", "dash", "phase"},
    "table": {"columns", "rows", "header"},
    "streaming_table": {
        "columns",
        "rows",
        "mode",
        "repeat_header",
        "sample_rows",
        "batch_size",
        "min_col_width",
        "max_col_width",
    },
    "link_url": {"url"},
    "link_page": {"page"},
    "link_named": {"destination"},
    **{kind: {"color"} for kind in ("highlight", "underline", "strikeout", "squiggly")},
    **{kind: {"text"} for kind in ("sticky_note", "sticky_note_at", "watermark", "stamp")},
    "watermark_confidential": set(),
    "watermark_draft": set(),
    "freetext": {"text", "width", "height"},
    "text_field": {"name", "width", "height", "value"},
    "checkbox": {"name", "width", "height", "checked"},
    "combo_box": {"name", "width", "height", "choices", "selected"},
    "push_button": {"name", "width", "height", "caption"},
    "signature_field": {"name", "width", "height"},
    "image": {"asset", "width", "height", "alt", "decorative"},
    "barcode": {"symbology", "data", "width", "height"},
    **{kind: set() for kind in EMPTY_TYPES},
    "space": {"points"},
}


def _element(item, page_width, fonts, images, budget):
    if not isinstance(item, dict):
        raise JobError("Page elements must be objects")
    kind = item.get("type")
    if not isinstance(kind, str):
        raise JobError("Creation element type must be a string")
    if kind in {"table", "streaming_table"}:
        if item.get("mode") == "auto_all":
            raise JobError("PDF Oxide 0.3.78 auto_all tables dropped every tested cell")
        raise JobError(
            "PDF Oxide 0.3.78 table text crosses cell borders in independent renders; "
            "table creation is blocked pending an upstream fix"
        )
    if kind == "radio_group":
        raise JobError(
            "PDF Oxide 0.3.78 radio-group creation produced unlinked widgets; "
            "this capability is blocked pending an upstream fix"
        )
    if kind in {"highlight", "underline", "strikeout", "squiggly"}:
        raise JobError(
            "PDF Oxide 0.3.78 text-markup quadrilaterals failed independent visual checks"
        )
    if kind == "stamp":
        raise JobError("PDF Oxide 0.3.78 stamp creation lacked a visible PDFium appearance")
    if kind == "link_named":
        raise JobError("Pinned Python creation cannot define a named link destination")
    common = {"type", "x", "y", "font", "size"}
    if kind not in ELEMENT_FIELDS:
        raise JobError("Unsupported creation element; scripts and arbitrary methods are disabled")
    _object(item, common | ELEMENT_FIELDS[kind], "creation element")
    if kind in TEXT_TYPES:
        _object(
            item,
            common
            | {"text", "level", "color", "count", "gap", "reference", "width", "height", "align"},
            "text element",
        )
        budget["text"] += len(_text(item.get("text", ""), MAX_CONTENT))
        if kind == "heading":
            _integer(item.get("level", 1), 1, 6, "heading level")
        elif kind == "columns":
            _integer(item.get("count", 2), 1, 4, "column count")
            _number(item.get("gap", 12), 0, 100, "column gap")
        elif kind == "footnote":
            _text(item.get("reference", "1"), 30)
        elif kind == "text_in_rect":
            _rect(item)
            _choice(item.get("align", "left"), ("left", "center", "right"), "alignment")
    elif kind in SHAPES:
        _object(
            item,
            common | {"width", "height", "x2", "y2", "color", "stroke", "dash", "phase"},
            "shape",
        )
        _rect(item)
        if "line" in kind:
            _number(item.get("x2", 200))
            _number(item.get("y2", 600))
        if kind.startswith("stroke"):
            _number(item.get("stroke", 1), 0.1, 50, "stroke width")
        if kind.endswith("dashed"):
            for value in _list(item.get("dash", [3, 2]), 16, "dash", 1):
                _number(value, 0.1, 100, "dash length")
            _number(item.get("phase", 0), 0, 100, "dash phase")
    elif kind in {"table", "streaming_table"}:
        _object(
            item,
            common
            | {
                "columns",
                "rows",
                "header",
                "mode",
                "repeat_header",
                "sample_rows",
                "batch_size",
                "min_col_width",
                "max_col_width",
            },
            "table",
        )
        _table(item, page_width, budget)
    elif kind in ANNOTATIONS:
        _object(
            item,
            common | {"text", "url", "page", "destination", "color", "width", "height"},
            "annotation",
        )
        budget["text"] += len(_text(item.get("text", "")))
        if kind == "link_url":
            _url(item.get("url"))
        elif kind == "link_page":
            _integer(item.get("page"), 1, MAX_PAGES, "destination page")
        elif kind == "link_named":
            _text(item.get("destination"), 100)
        elif kind == "freetext":
            _rect(item)
    elif kind in FORM_TYPES:
        _object(
            item,
            common
            | {
                "name",
                "width",
                "height",
                "value",
                "checked",
                "choices",
                "selected",
                "buttons",
                "caption",
            },
            "form field",
        )
        name = _text(item.get("name"), 100, "field name")
        if not name or name in budget["fields"]:
            raise JobError("Form field names must be nonempty and unique")
        budget["fields"].add(name)
        _rect(item)
        if kind == "text_field":
            _text(item.get("value", ""))
        elif kind == "checkbox":
            _boolean(item.get("checked", False), "checked")
        elif kind == "combo_box":
            choices = [_text(v, 200) for v in _list(item.get("choices"), 100, "choices", 1)]
            if item.get("selected") is not None and item["selected"] not in choices:
                raise JobError("Selected combo value must be one of its choices")
        elif kind == "radio_group":
            values = []
            for button in _list(item.get("buttons"), 100, "radio buttons", 1):
                _object(button, ("value", "x", "y", "width", "height"), "radio button")
                value = _text(button.get("value"), 200)
                if not value or value in values:
                    raise JobError("Radio export values must be unique and nonempty")
                values.append(value)
                _rect(button)
            if item.get("selected") is not None and item["selected"] not in values:
                raise JobError("Selected radio value must identify a button")
        elif kind == "push_button":
            _text(item.get("caption", ""), 200)
    elif kind == "image":
        _object(item, common | {"asset", "width", "height", "alt", "decorative"}, "image")
        if _text(item.get("asset"), 80, "image asset") not in images:
            raise JobError("Image element must name an embedded image asset")
        _rect(item)
        _text(item.get("alt", ""), 1000)
        _boolean(item.get("decorative", False), "decorative")
        if not item.get("decorative", False) and not item.get("alt", ""):
            raise JobError("Nondecorative image requires alt text")
        if item.get("decorative", False) and item.get("alt", ""):
            raise JobError("Decorative images cannot also declare semantic alt text")
    elif kind == "barcode":
        _object(item, common | {"symbology", "data", "width", "height"}, "barcode")
        _choice(item.get("symbology", "qr"), (*BARCODES, "qr"), "barcode symbology")
        _text(item.get("data"), 1024, "barcode data")
        _rect(item)
        if item.get("symbology", "qr") == "qr":
            _number(item.get("width", 100), 21, 1024, "QR size")
            if "height" in item and item["height"] != item.get("width", 100):
                raise JobError("QR codes require equal width and height")
        else:
            _number(item.get("width", 100), 8, 1024, "barcode width")
            _number(item.get("height", 30), 8, 1024, "barcode height")
    elif kind in EMPTY_TYPES:
        _object(item, common, "layout break")
        if kind == "new_page":
            budget["pages"] += 1
    elif kind == "space":
        _object(item, common | {"points"}, "spacing")
        _number(item.get("points", 12), 0, 500, "space")
    else:
        raise JobError("Unsupported creation element; scripts and arbitrary methods are disabled")
    if "font" in item:
        _choice(item["font"], (*BASE_FONTS, *fonts), "font")
    if "size" in item:
        _number(item["size"], 4, 144, "font size")
    for key in ("x", "y"):
        if key in item:
            _number(item[key])
    if "color" in item:
        _color(item["color"])


def _document(value):
    _text(value, MAX_SPEC_BYTES, "document JSON")
    if len(value.encode()) > MAX_SPEC_BYTES:
        raise JobError("Document declaration byte limit exceeded")
    document = _object(
        strict_json_loads(value),
        ("metadata", "tagged", "language", "role_map", "fonts", "images", "pages"),
        "document",
    )
    metadata = _object(
        document.get("metadata", {}),
        ("title", "author", "subject", "keywords", "creator"),
        "metadata",
    )
    for value in metadata.values():
        _text(value, 500, "metadata")
    _boolean(document.get("tagged", False), "tagged")
    language = _text(document.get("language", "en-US"), 35, "language")
    if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", language):
        raise JobError("Language must be a bounded language tag")
    roles = document.get("role_map", {})
    if not isinstance(roles, dict) or len(roles) > 32:
        raise JobError("Role map exceeds its limit")
    for custom, standard in roles.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,39}", custom):
            raise JobError("Invalid structure role name")
        _choice(
            standard,
            (
                "Document",
                "Part",
                "Sect",
                "Div",
                "P",
                "H",
                "H1",
                "H2",
                "H3",
                "H4",
                "H5",
                "H6",
                "L",
                "LI",
                "Lbl",
                "LBody",
                "Table",
                "TR",
                "TH",
                "TD",
                "Span",
                "Quote",
                "Note",
                "Reference",
                "Link",
                "Figure",
                "Form",
            ),
            "standard structure role",
        )
    if not document.get("tagged", False) and (roles or "language" in document):
        raise JobError("Language and role maps require tagged PDF creation")
    fonts, images = (
        _assets(document.get("fonts", []), "fonts"),
        _assets(document.get("images", []), "images"),
    )
    if sum(map(len, fonts.values())) + sum(map(len, images.values())) > MAX_ASSET_BYTES:
        raise JobError("Combined embedded asset byte limit exceeded")
    pages = _list(document.get("pages"), MAX_PAGES, "pages", 1)
    budget = {"pages": len(pages), "elements": 0, "text": 0, "cells": 0, "fields": set()}
    for page in pages:
        _object(page, ("size", "elements"), "page")
        size = page.get("size", "a4")
        if isinstance(size, list):
            width, height = _list(size, 2, "page dimensions", 2)
            _number(width, 180, 2000, "page width")
            _number(height, 180, 2000, "page height")
        else:
            _choice(size, ("a4", "letter"), "paper size")
            width = 595 if size == "a4" else 612
        elements = _list(page.get("elements", []), MAX_ELEMENTS, "elements")
        budget["elements"] += len(elements)
        for item in elements:
            _element(item, width, fonts, images, budget)
    if (
        budget["elements"] > MAX_ELEMENTS
        or budget["text"] > MAX_CONTENT
        or budget["pages"] > MAX_PAGES
    ):
        raise JobError("Document creation content/page limit exceeded")
    return document, fonts, images


def validate_options(operation, options):
    """Called before spawn and again by the worker; does not import native parsers."""
    if operation == "oxide_create_document":
        _document(options["document"])
    elif operation == "oxide_create_text":
        kind = options["format"]
        _choice(kind, ("text", "markdown", "html", "html_css"), "source format")
        _text(options["content"], MAX_CONTENT)
        _markup_content(options["content"], kind)
        _css(options["css"])
        fonts = _assets(strict_json_loads(options["fonts"]), "fonts")
        if kind == "html_css":
            if not fonts:
                raise JobError("HTML/CSS creation requires at least one embedded font")
            if options["title"] or options["author"]:
                raise JobError("Pinned HTML/CSS creation has no title/author arguments")
        elif fonts or options["css"]:
            raise JobError("CSS and font assets require HTML/CSS mode")
        if kind != "markdown" and any(
            options.get(key) for key in ("header", "footer", "header_left", "footer_left")
        ):
            raise JobError("Header/footer templates require Markdown mode")
        if len(str(options).encode()) > MAX_SPEC_BYTES:
            raise JobError("Creation request byte limit exceeded")
    elif operation == "oxide_create_barcode":
        _text(options["data"], 1024, "barcode data")
        if not options["data"]:
            raise JobError("Barcode data must be nonempty")
        if options["symbology"] != "qr" and (
            options["size"] != 256 or options["error_correction"] != "medium"
        ):
            raise JobError("Size and error correction apply only to QR SVG")
    elif operation == "oxide_create_pattern":
        for key in ("color", "secondary"):
            if not re.fullmatch(r"#[0-9A-Fa-f]{6}", options[key]):
                raise JobError("Pattern colors require #RRGGBB")
        if options["detail"] > options["size"] / 2:
            raise JobError("Pattern detail must not exceed half its cell size")
        if options["preset"] == "checkerboard" and options["detail"] != 2:
            raise JobError("Checkerboard does not consume a stripe/radius/line detail option")
        if options["preset"] != "checkerboard" and options["secondary"] != "#eeeeee":
            raise JobError("Secondary color applies only to checkerboard")
    else:
        raise JobError("Unknown PDF Oxide creation operation")
    return options


def _checked_assets(fonts, images, native):
    """Decode only after process bounds. Pillow inspects dimensions before load()."""
    import pymupdf
    from PIL import Image

    font_checks = {}
    for name, data in fonts.items():
        font_checks[name] = native(
            "font coverage inspection", lambda d=data: pymupdf.Font(fontbuffer=d)
        )
    pixels, reports = 0, []
    for name, data in images.items():

        def inspect_image(data=data):
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or image.n_frames != 1:
                    raise JobError("Only single-frame PNG, JPEG and WebP assets are supported")
                width, height = image.size
                if not 0 < width * height <= MAX_IMAGE_PIXELS:
                    raise JobError("Embedded image decoded pixel limit exceeded")
                image.verify()
                return width, height, image.format

        width, height, kind = native("embedded image validation", inspect_image)
        pixels += width * height
        if pixels > MAX_TOTAL_PIXELS:
            raise JobError("Embedded images exceed the total decoded pixel limit")
        reports.append(
            {
                "name": name,
                "format": kind,
                "width": width,
                "height": height,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return font_checks, reports


def _glyphs(text, name, font_checks):
    if name in font_checks:
        if any(not font_checks[name].has_glyph(ord(char)) for char in text if not char.isspace()):
            raise JobError("Embedded font lacks a requested glyph; output was not published")
    else:
        try:
            text.encode("cp1252")
        except UnicodeEncodeError:
            raise JobError(
                "Text outside WinAnsi requires an embedded font in the page declaration"
            ) from None


def _render_table(page, item, oxide):
    columns = [
        oxide.Column(c.get("header", ""), c.get("width", 100), c.get("align", "left"))
        for c in item["columns"]
    ]
    if item["type"] == "table":
        page.table(oxide.Table(columns, item["rows"], has_header=item.get("header", False)))
        return
    rows = item["rows"]
    rowspan = max(
        (cell.get("rowspan", 1) for row in rows for cell in row if isinstance(cell, dict)),
        default=1,
    )
    table = page.streaming_table(
        columns,
        repeat_header=item.get("repeat_header", False),
        mode=item.get("mode", "fixed"),
        sample_rows=item.get("sample_rows", 50),
        min_col_width_pt=item.get("min_col_width", 20),
        max_col_width_pt=item.get("max_col_width", 400),
        max_rowspan=rowspan,
        batch_size=item.get("batch_size", 128),
    )
    for row in rows:
        if any(isinstance(cell, dict) for cell in row):
            table.push_row_span(
                [
                    (cell["text"], cell.get("rowspan", 1)) if isinstance(cell, dict) else (cell, 1)
                    for cell in row
                ]
            )
        else:
            table.push_row(row)
    table.flush()
    table.finish()


def _render_element(page, item, oxide, images):
    kind = item["type"]
    text = item.get("text", "")
    color = tuple(item.get("color", [0, 0, 0]))
    x, y, width, height = _rect(item)
    match kind:
        case "text":
            page.text(text)
        case "heading":
            page.heading(item.get("level", 1), text)
        case "paragraph":
            page.paragraph(text)
        case "inline":
            page.inline(text)
        case "inline_bold":
            page.inline_bold(text)
        case "inline_italic":
            page.inline_italic(text)
        case "inline_color":
            page.inline_color(*color, text)
        case "columns":
            page.columns(item.get("count", 2), item.get("gap", 12), text)
        case "footnote":
            page.footnote(item.get("reference", "1"), text)
        case "text_in_rect":
            page.text_in_rect(x, y, width, height, text, item.get("align", "left"))
        case "rect":
            page.rect(x, y, width, height)
        case "filled_rect":
            page.filled_rect(x, y, width, height, *color)
        case "line":
            page.line(x, y, item.get("x2", 200), item.get("y2", 600))
        case "stroke_rect":
            page.stroke_rect(x, y, width, height, item.get("stroke", 1), color)
        case "stroke_rect_dashed":
            page.stroke_rect_dashed(
                x,
                y,
                width,
                height,
                item.get("dash", [3, 2]),
                item.get("stroke", 1),
                color,
                item.get("phase", 0),
            )
        case "stroke_line":
            page.stroke_line(
                x, y, item.get("x2", 200), item.get("y2", 600), item.get("stroke", 1), color
            )
        case "stroke_line_dashed":
            page.stroke_line_dashed(
                x,
                y,
                item.get("x2", 200),
                item.get("y2", 600),
                item.get("dash", [3, 2]),
                item.get("stroke", 1),
                color,
                item.get("phase", 0),
            )
        case "table" | "streaming_table":
            _render_table(page, item, oxide)
        case "horizontal_rule":
            page.horizontal_rule()
        case "newline":
            page.newline()
        case "new_page":
            page.new_page_same_size()
        case "space":
            page.space(item.get("points", 12))
        case "link_url":
            page.link_url(item["url"])
        case "link_page":
            page.link_page(item["page"] - 1)
        case "link_named":
            page.link_named(item["destination"])
        case "highlight":
            page.highlight(color)
        case "underline":
            page.underline(color)
        case "strikeout":
            page.strikeout(color)
        case "squiggly":
            page.squiggly(color)
        case "sticky_note":
            page.sticky_note(text)
        case "sticky_note_at":
            page.sticky_note_at(x, y, text)
        case "watermark":
            page.watermark(text)
        case "watermark_confidential":
            page.watermark_confidential()
        case "watermark_draft":
            page.watermark_draft()
        case "stamp":
            page.stamp(text)
        case "freetext":
            page.freetext(x, y, width, height, text)
        case "text_field":
            page.text_field(item["name"], x, y, width, height, item.get("value", ""))
        case "checkbox":
            page.checkbox(item["name"], x, y, width, height, item.get("checked", False))
        case "combo_box":
            page.combo_box(item["name"], x, y, width, height, item["choices"], item.get("selected"))
        case "radio_group":
            buttons = [(b["value"], *_rect(b)) for b in item["buttons"]]
            page.radio_group(item["name"], buttons, item.get("selected"))
        case "push_button":
            page.push_button(item["name"], x, y, width, height, item.get("caption", ""))
        case "signature_field":
            page.signature_field(item["name"], x, y, width, height)
        case "image":
            data = images[item["asset"]]
            if item.get("decorative", False):
                page.image_artifact(data, x, y, width, height)
            else:
                page.image_with_alt(data, x, y, width, height, item["alt"])
        case "barcode":
            if item.get("symbology", "qr") == "qr":
                page.barcode_qr(item["data"], x, y, width)
            else:
                page.barcode_1d(BARCODES[item["symbology"]], item["data"], x, y, width, height)


def _build(document, fonts, images, oxide, native):
    font_checks, image_reports = _checked_assets(fonts, images, native)
    builder = oxide.DocumentBuilder()
    metadata = document.get("metadata", {})
    # Fixed explicit metadata API; no method chosen by untrusted input.
    for key, setter in (
        ("title", builder.title),
        ("author", builder.author),
        ("subject", builder.subject),
        ("keywords", builder.keywords),
        ("creator", builder.creator),
    ):
        if key in metadata:
            setter(metadata[key])
    for name, data in fonts.items():
        builder.register_embedded_font(name, oxide.EmbeddedFont.from_bytes(data, name=name))
    if document.get("tagged", False):
        builder.tagged_pdf_ua1().language(document.get("language", "en-US"))
        for custom, standard in document.get("role_map", {}).items():
            builder.role_map(custom, standard)
    metrics, warning = [], []
    for number, declaration in enumerate(document["pages"], 1):
        size = declaration.get("size", "a4")
        dimensions = (595, 842) if size == "a4" else (612, 792) if size == "letter" else size
        page = (
            builder.a4_page()
            if size == "a4"
            else builder.letter_page()
            if size == "letter"
            else builder.page(*size)
        )
        font, font_size = "Helvetica", 12
        for item in declaration.get("elements", []):
            font, font_size = item.get("font", font), item.get("size", font_size)
            page.font(font, font_size)
            if "x" in item or "y" in item:
                page.at(item.get("x", 72), item.get("y", 600))
            for key in ("text", "reference", "value", "caption"):
                if key in item:
                    _glyphs(item[key], font, font_checks)
            if item["type"] in {"table", "streaming_table"}:
                for col in item["columns"]:
                    _glyphs(col.get("header", ""), font, font_checks)
                for row in item["rows"]:
                    for cell in row:
                        _glyphs(cell["text"] if isinstance(cell, dict) else cell, font, font_checks)
            if item["type"] == "text":
                metrics.append(
                    {
                        "declared_page": number,
                        "text_sha256": hashlib.sha256(item.get("text", "").encode()).hexdigest(),
                        "width_pt": page.measure(item.get("text", "")),
                        "remaining_space_estimate_pt": page.remaining_space(),
                        "metric_font": "Helvetica fallback" if font in fonts else font,
                    }
                )
            if item["type"].startswith("watermark"):
                _watermark_bounds(item, dimensions)
            _render_element(page, item, oxide, images)
        page.done()
    if document.get("tagged", False):
        warning.append(
            "Tagged PDF metadata was requested; PDF/UA conformance has not been certified."
        )
    if any(
        item["type"] == "streaming_table"
        for p in document["pages"]
        for item in p.get("elements", [])
    ):
        warning.append(
            "Pinned Python streaming tables buffer all rows; total rows/cells are bounded."
        )
    data = bytes(builder.build())
    targets = [
        item["page"]
        for p in document["pages"]
        for item in p.get("elements", [])
        if item["type"] == "link_page"
    ]
    if targets and max(targets) > int(oxide.PdfDocument.from_bytes(data).page_count):
        raise JobError("Internal link destination exceeds the generated page count")
    initial_values = {
        item["name"]: item.get("value", "")
        for page in document["pages"]
        for item in page.get("elements", [])
        if item["type"] == "text_field"
    }
    if initial_values:
        # The builder writes /DV but omits /V. Apply the declared initial value
        # through the pinned edit API; do not report a blank live field as filled.
        editable = oxide.PdfDocument.from_bytes(data)
        for name, value in initial_values.items():
            editable.set_form_field_value(name, value)
        data = bytes(editable.to_bytes())
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as check:
            values = {w.field_name: w.field_value for page in check for w in page.widgets() or ()}
        if any(values.get(name) != value for name, value in initial_values.items()):
            raise JobError("Created text field did not retain its declared initial value")
        warning.append("Text-field current values were set separately after native creation.")
    return data, {"images": image_reports, "text_metrics": metrics}, warning


def _watermark_bounds(item, dimensions):
    """Reject native fixed-size watermark text that its own appearance clips."""
    import pymupdf

    kind = item["type"]
    content = (
        "DRAFT"
        if kind == "watermark_draft"
        else "CONFIDENTIAL"
        if kind == "watermark_confidential"
        else item["text"]
    )
    _glyphs(content, "Helvetica", {})
    font_size = 72 if kind == "watermark" else 48
    font = pymupdf.Font(fontname="helv")
    width, height = dimensions[0] * 0.8, dimensions[1] * 0.4
    x = max((width - len(content.encode()) * font_size * 0.5) / 2, 0)
    y = max((height - font_size) / 2, 0)
    right = x + font.text_length(content, fontsize=font_size)
    bottom, top = y + font.descender * font_size, y + font.ascender * font_size
    for px, py in ((x, bottom), (x, top), (right, bottom), (right, top)):
        dx, dy = px - width / 2, py - height / 2
        rx, ry = (dx - dy) / math.sqrt(2) + width / 2, (dx + dy) / math.sqrt(2) + height / 2
        if not 1 <= rx <= width - 1 or not 1 <= ry <= height - 1:
            raise JobError(
                "Native watermark would clip within its appearance; "
                "use a larger page or shorter text"
            )


def _create_text(options, oxide, native):
    kind, content = options["format"], options["content"]
    title, author = options["title"] or None, options["author"] or None
    if kind == "text":
        _glyphs(content, "Helvetica", {})
        measure = oxide.DocumentBuilder().a4_page()
        if any(measure.measure(line) > 451 for line in content.splitlines()):
            raise JobError("Plain-text line exceeds the native page width; wrap it explicitly")
        pdf = oxide.Pdf.from_text(content, title=title, author=author)
    elif kind == "markdown":
        if any(options.get(key) for key in ("header", "footer", "header_left", "footer_left")):
            template = oxide.PageTemplate()
            if options.get("header_left"):
                template.header(
                    oxide.Artifact.center(options["header"]).with_left(options["header_left"])
                )
            elif options["header"]:
                template.header(oxide.Header.center(options["header"]))
            if options.get("footer_left"):
                template.footer(
                    oxide.Artifact.center(options["footer"]).with_left(options["footer_left"])
                )
            elif options["footer"]:
                template.footer(oxide.Footer.center(options["footer"]))
            pdf = oxide.Pdf.from_markdown_with_template(
                content, template, title=title, author=author
            )
        else:
            pdf = oxide.Pdf.from_markdown(content, title=title, author=author)
    elif kind == "html":
        pdf = oxide.Pdf.from_html(content, title=title, author=author)
    else:
        fonts = _assets(strict_json_loads(options["fonts"]), "fonts")
        checks, _ = _checked_assets(fonts, {}, native)
        plain = html.unescape(re.sub(r"<[^>]*>", "", content))
        if any(
            not any(font.has_glyph(ord(char)) for font in checks.values())
            for char in plain
            if not char.isspace()
        ):
            raise JobError("HTML/CSS fonts lack a requested glyph; output was not published")
        if len(fonts) == 1:
            pdf = oxide.Pdf.from_html_css(content, options["css"], next(iter(fonts.values())))
        else:
            pdf = oxide.Pdf.from_html_css_with_fonts(content, options["css"], list(fonts.items()))
    return bytes(pdf.to_bytes())


def _standalone(request, options, add, oxide, native, write_bytes, write_json):
    import json

    if request["operation"] == "oxide_create_barcode":
        if options["symbology"] == "qr":
            correction = ("low", "medium", "quartile", "high").index(options["error_correction"])
            svg = native(
                "QR SVG generation",
                lambda: oxide.generate_qr_svg(options["data"], correction, options["size"]),
            )
        else:
            svg = native(
                "barcode SVG generation",
                lambda: oxide.generate_barcode_svg(BARCODES[options["symbology"]], options["data"]),
            )
        data = svg.encode("utf-8")
        if len(data) > 2 * 1024**2:
            raise JobError("Generated barcode SVG exceeds its byte limit")
        from defusedxml import ElementTree

        root = native("generated SVG validation", lambda: ElementTree.fromstring(data))
        nodes = list(root.iter())
        if len(nodes) > 30_000 or root.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
            raise JobError("Unexpected generated SVG structure")
        for node in nodes:
            if node.tag not in {"svg", "g", "rect"} | {
                f"{{http://www.w3.org/2000/svg}}{tag}" for tag in ("svg", "g", "rect")
            }:
                raise JobError("Generated SVG contains an unsupported element")
            if node.attrib.keys() - {"x", "y", "width", "height", "fill", "viewBox", "version"}:
                raise JobError("Generated SVG contains an unsupported attribute")
            if any("url" in value.lower() or ":" in value for value in node.attrib.values()):
                raise JobError("Generated SVG contains an unexpected resource reference")
        raw_hash = hashlib.sha256(data).hexdigest()
        namespace_added = root.tag == "svg"
        if namespace_added:
            # Pinned 1D generator omits the SVG namespace. Retain its original
            # bytes separately and add only the app-owned namespace for browsers.
            write_bytes(request, add, "oxide-barcode-native.txt", data)
            import xml.etree.ElementTree as xml_writer

            root.set("xmlns", "http://www.w3.org/2000/svg")
            data = xml_writer.tostring(root, encoding="utf-8")
        filename = "oxide-barcode.svg"
        details = {
            "symbology": options["symbology"],
            "payload_sha256": hashlib.sha256(options["data"].encode()).hexdigest(),
            "decoded_payload_verified": False,
            "native_sha256": raw_hash,
            "svg_namespace_added": namespace_added,
        }
    else:
        size, detail = options["size"], options["detail"]
        color = oxide.Color.from_hex(options["color"])
        secondary = oxide.Color.from_hex(options["secondary"])
        presets = oxide.PatternPresets
        match options["preset"]:
            case "horizontal_stripes":
                result = native(
                    "pattern generation",
                    lambda: presets.horizontal_stripes(size, size, detail, color),
                )
            case "vertical_stripes":
                result = native(
                    "pattern generation",
                    lambda: presets.vertical_stripes(size, size, detail, color),
                )
            case "checkerboard":
                result = native(
                    "pattern generation", lambda: presets.checkerboard(size, color, secondary)
                )
            case "dots":
                result = native("pattern generation", lambda: presets.dots(size, detail, color))
            case "diagonal_lines":
                result = native(
                    "pattern generation", lambda: presets.diagonal_lines(size, detail, color)
                )
            case "crosshatch":
                result = native(
                    "pattern generation", lambda: presets.crosshatch(size, detail, color)
                )
        data = bytes(result)
        if len(data) > 100_000:
            raise JobError("Generated pattern content exceeds its byte limit")
        filename = "oxide-pattern-content.txt"
        details = {
            "preset": options["preset"],
            "rgb": [color.r, color.g, color.b],
            "complete_pdf": False,
            "page_pattern_applied": False,
        }
    write_bytes(request, add, filename, data)
    write_json(
        request,
        add,
        "oxide-creation.json",
        {
            "schema": "pdf-multitool.oxide-creation.v1",
            "engine": "pdf-oxide",
            "version": "0.3.78",
            "operation": request["operation"],
            "source_sha256": hashlib.sha256(
                json.dumps(options, sort_keys=True).encode()
            ).hexdigest(),
            "output_sha256": hashlib.sha256(data).hexdigest(),
            "output_bytes": len(data),
            "truncated": False,
            "review_required": True,
            **details,
        },
    )
    return ["Generated content requires independent application/decoder review."], False


def process(request, options, add):
    from .oxide_runtime import native, require_runtime, write_bytes, write_json

    oxide = require_runtime(request)
    validate_options(request["operation"], options)
    if request["operation"] in {"oxide_create_barcode", "oxide_create_pattern"}:
        return _standalone(request, options, add, oxide, native, write_bytes, write_json)
    if request["operation"] == "oxide_create_document":
        document, fonts, images = _document(options["document"])
        data, details, warnings = native(
            "document creation", lambda: _build(document, fonts, images, oxide, native)
        )
        source = options["document"]
    else:
        data = native("markup creation", lambda: _create_text(options, oxide, native))
        details, warnings, source = {}, [], options["content"]
    if len(data) > min(request["limits"]["max_output_bytes"], 16 * 1024**2):
        raise JobError("Created PDF exceeds its output limit; no output published")
    output = native("created PDF validation", lambda: oxide.PdfDocument.from_bytes(data))
    count = native("created page count", lambda: int(output.page_count))
    if not 1 <= count <= MAX_PAGES:
        raise JobError("Created PDF exceeds its page limit; no output published")
    warnings.append(
        "Generated PDF requires visual/text checks; creation does not certify fidelity."
    )
    write_bytes(request, add, "oxide-created.pdf", data)
    write_json(
        request,
        add,
        "oxide-creation.json",
        {
            "schema": "pdf-multitool.oxide-creation.v1",
            "engine": "pdf-oxide",
            "version": "0.3.78",
            "operation": request["operation"],
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "output_sha256": hashlib.sha256(data).hexdigest(),
            "pages": count,
            "output_bytes": len(data),
            "truncated": False,
            "review_required": True,
            "warnings": warnings,
            **details,
        },
    )
    return warnings, False
