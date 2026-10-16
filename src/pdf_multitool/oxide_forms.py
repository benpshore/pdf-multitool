"""Bounded PDF Oxide form and annotation workflows, preserving the source PDF."""

from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .core import JobError, strict_json_loads

MAX_CONFIG = 32 * 1024
MAX_FIELDS = 1000
MAX_ANNOTATIONS = 2000
MAX_REPORT = 2 * 1024**2
MAX_EDITS = 100
MAX_DOCUMENT_PAGES = 100


def _field(label, kind="string", default="", **kwargs):
    return {"label": label, "type": kind, "default": default, **kwargs}


def _operation(label, description, *, options=None, pages=False, pdf=False, extra=None):
    return {
        "label": label,
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": pages,
        "description": description,
        "outputs": ["application/json", *(["application/pdf"] if pdf else []), *(extra or [])],
        "options": options or {},
    }


OPERATIONS = {
    "oxide_form_inspect": _operation(
        "Inspect forms with PDF Oxide",
        "Read AcroForm fields and widget page identities; detect XFA presence. "
        "XFA content and form appearance are not interpreted.",
    ),
    "oxide_form_fill": _operation(
        "Fill forms with PDF Oxide",
        "Set existing AcroForm values in a new PDF and check saved values. "
        "Review appearances; scripts are not executed. XFA and signature fields are unsupported.",
        pdf=True,
        options={
            "values": _field(
                "Field values (JSON object)", default="{}", multiline=True, maxLength=MAX_CONFIG
            )
        },
    ),
    "oxide_form_export": _operation(
        "Export form data with PDF Oxide",
        "Export existing AcroForm values as FDF or XFDF plus the exact JSON field report. "
        "Exports contain data, not form appearances. XFA export is unsupported. "
        "FDF Unicode and XFDF array/empty-string values fail because pinned exports lose fidelity.",
        extra=["application/vnd.fdf", "application/vnd.adobe.xfdf"],
        options={"format": _field("Format", default="xfdf", enum=["fdf", "xfdf"])},
    ),
    "oxide_form_flatten": _operation(
        "Flatten forms with PDF Oxide",
        "Make selected form widgets noninteractive in a new PDF. "
        "Values without usable appearances may disappear; inspect the original and result. "
        "XFA is unsupported and signatures become invalid.",
        pages=True,
        pdf=True,
        options={
            "allow_flatten": _field(
                "Allow form flattening and appearance changes", "boolean", False
            )
        },
    ),
    "oxide_annotation_inspect": _operation(
        "Inspect annotations with PDF Oxide",
        "Read annotation metadata and native rectangles by source page. "
        "Links remain inert JSON strings; destinations are not fetched.",
        pages=True,
    ),
    "oxide_annotation_edit": _operation(
        "Edit annotations with PDF Oxide",
        "Add a note or HTTP(S)/mailto link, or remove a non-widget annotation "
        "in a new PDF using a bounded JSON action list. Coordinates use native PDF user space. "
        "Removal fails closed when the saved annotation remains. Native highlights stay blocked. "
        "Optional pikepdf correction builds the requested rectangular highlight appearance; "
        "both the defective native copy and corrected copy are retained for comparison.",
        pdf=True,
        options={
            "actions": _field(
                "Annotation actions (JSON array)",
                default="[]",
                multiline=True,
                maxLength=MAX_CONFIG,
            ),
            "allow_highlight_repair": _field(
                "Correct new highlight appearances with pikepdf and retain native comparison",
                "boolean",
                False,
            ),
        },
    ),
    "oxide_annotation_flatten": _operation(
        "Flatten annotations with PDF Oxide",
        "Flatten selected page annotations into a new PDF. "
        "Links and interactivity are removed; missing appearances may lose visible content. "
        "Widget-bearing pages require the form flattening operation first.",
        pages=True,
        pdf=True,
        options={
            "allow_flatten": _field(
                "Allow annotation flattening and appearance changes", "boolean", False
            )
        },
    ),
}


def _json_config(text, kind):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CONFIG:
        raise JobError("PDF Oxide form/annotation JSON exceeds its byte limit")
    try:
        value = strict_json_loads(text)
    except (ValueError, RecursionError) as exc:
        raise JobError("Invalid PDF Oxide form/annotation JSON") from exc
    if not isinstance(value, kind):
        raise JobError(f"PDF Oxide configuration must be a JSON {kind.__name__}")
    return value


def _text(value, maximum):
    return isinstance(value, str) and len(value) <= maximum and "\x00" not in value


def _number(value):
    return type(value) in {int, float} and math.isfinite(value) and abs(value) <= 100_000


def _validate_values(text):
    values = _json_config(text, dict)
    if not 1 <= len(values) <= MAX_EDITS:
        raise JobError("Set between 1 and 100 form fields per request")
    for name, value in values.items():
        if not _text(name, 240) or not name:
            raise JobError("Form field names must be nonempty strings of at most 240 characters")
        if value is None or type(value) is bool or _text(value, 10_000):
            continue
        if isinstance(value, list) and len(value) <= 100 and all(_text(v, 1000) for v in value):
            continue
        raise JobError("Form values must be null, booleans, bounded strings or lists of strings")
    return values


def _validate_actions(text, allow_highlight_repair=False):
    actions = _json_config(text, list)
    if not 1 <= len(actions) <= MAX_EDITS:
        raise JobError("Provide between 1 and 100 annotation actions")
    schemas = {
        "note": {"type", "page", "x", "y", "text"},
        "highlight": {"type", "page", "x", "y", "width", "height", "color"},
        "link": {"type", "page", "x", "y", "width", "height", "url"},
        "remove": {"type", "page", "index"},
    }
    removed = set()
    for action in actions:
        if not isinstance(action, dict) or action.get("type") not in schemas:
            raise JobError("Unsupported PDF Oxide annotation action")
        if action.keys() != schemas[action["type"]]:
            raise JobError("Annotation action has missing or unknown properties")
        if type(action["page"]) is not int or not 1 <= action["page"] <= MAX_DOCUMENT_PAGES:
            raise JobError("Annotation page must be a one-based integer from 1 to 100")
        if action["type"] == "remove":
            if type(action["index"]) is not int or not 0 <= action["index"] < MAX_ANNOTATIONS:
                raise JobError("Annotation index must be a bounded zero-based original page index")
            key = (action["page"], action["index"])
            if key in removed:
                raise JobError("Duplicate annotation removal")
            removed.add(key)
            continue
        if not all(_number(action[key]) for key in ("x", "y")):
            raise JobError("Annotation coordinates must be bounded finite numbers")
        if action["type"] == "note":
            if not _text(action["text"], 10_000):
                raise JobError("Annotation note exceeds its text limit")
            continue
        if not all(_number(action[key]) and action[key] > 0 for key in ("width", "height")):
            raise JobError("Annotation width and height must be bounded positive numbers")
        if action["type"] == "highlight":
            color = action["color"]
            if (
                not isinstance(color, list)
                or len(color) != 3
                or not all(_number(channel) and 0 <= channel <= 1 for channel in color)
            ):
                raise JobError("Annotation color must contain three numbers from zero to one")
            if allow_highlight_repair is not True:
                raise JobError(
                    "PDF Oxide 0.3.78 new highlights are unsupported: "
                    "the saved appearance disappears in PDFium. "
                    "Explicit allow_highlight_repair uses a separate pikepdf correction"
                )
            if min(action["width"], action["height"]) < 0.5:
                raise JobError("Corrected highlight dimensions must be at least 0.5 PDF points")
        else:
            url = action["url"]
            if not _text(url, 2048) or any(ord(char) < 32 for char in url):
                raise JobError("Invalid annotation link")
            try:
                parsed = urlsplit(url)
                valid = (
                    parsed.scheme in {"https", "http", "mailto"}
                    and (bool(parsed.netloc) if parsed.scheme != "mailto" else bool(parsed.path))
                    and not parsed.username
                    and not parsed.password
                )
            except ValueError:
                valid = False
            if not valid:
                raise JobError("Annotation links require HTTP(S) or mailto without credentials")
    if allow_highlight_repair and not any(a["type"] == "highlight" for a in actions):
        raise JobError("Highlight repair requires an explicit new highlight action")
    return actions


def validate_options(operation, options):
    """Called before spawn and again inside the worker; no document/parser access."""
    if operation == "oxide_form_fill":
        _validate_values(options["values"])
    elif operation == "oxide_annotation_edit":
        _validate_actions(options["actions"], options.get("allow_highlight_repair", False))
    if operation.endswith("_flatten") and options.get("allow_flatten") is not True:
        raise JobError("Explicit allow_flatten is required for PDF Oxide flattening")


def _bounded_report(value):
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_REPORT:
        raise JobError("PDF Oxide form/annotation report exceeds its output limit")
    return value


def _annotations(doc, indices):
    from .oxide_runtime import native

    pages, count = [], 0
    for index in indices:
        annotations = native("annotation inspection", lambda i=index: doc.get_annotations(i))
        count += len(annotations)
        if count > MAX_ANNOTATIONS:
            raise JobError("PDF Oxide annotation count limit exceeded")
        pages.append(
            {
                "page": index + 1,
                "native_page_index": index,
                "annotations": [{"index": n, **item} for n, item in enumerate(annotations)],
            }
        )
        _bounded_report(pages)
    return pages


def _fields(doc):
    from .oxide_runtime import native

    values = native("form field inspection", doc.get_form_fields)
    if len(values) > MAX_FIELDS:
        raise JobError("PDF Oxide form field count limit exceeded")
    fields = []
    for field in values:
        fields.append(
            {
                "name": field.name,
                "field_type": field.field_type,
                "value": field.value,
                "tooltip": field.tooltip,
                "bounds": field.bounds,
                "flags": field.flags,
                "max_length": field.max_length,
                "is_readonly": field.is_readonly,
                "is_required": field.is_required,
            }
        )
    _bounded_report(fields)
    return fields


def _diagnostics(doc):
    from .oxide_runtime import native

    records = native("form/annotation diagnostics", doc.structured_warnings)
    if len(records) > MAX_ANNOTATIONS:
        raise JobError("PDF Oxide diagnostic count limit exceeded")
    return _bounded_report(records)


def _reopen(data, pages):
    from pdf_oxide import PdfDocument

    from .oxide_runtime import native, validate_pdf_bytes

    if not isinstance(data, bytes) or len(data) > 16 * 1024**2:
        raise JobError("PDF Oxide saved document exceeds its output byte limit")
    validate_pdf_bytes(data, pages)
    check = native("saved PDF validation", lambda: PdfDocument.from_bytes(data))
    if native("saved page count", lambda: int(check.page_count)) != pages:
        raise JobError("PDF Oxide saved PDF page count differs from its source")
    return check


def _metadata_matches(actual, expected):
    """Keep strings exact; tolerate native f32 serialization rounding within 0.001pt."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _metadata_matches(actual[key], value)
            for key, value in expected.items()
            if key != "index"
        )
    if isinstance(expected, list | tuple):
        return (
            isinstance(actual, list | tuple)
            and len(actual) == len(expected)
            and all(_metadata_matches(a, b) for a, b in zip(actual, expected, strict=True))
        )
    if type(expected) in {float, int} and type(actual) in {float, int}:
        return math.isclose(actual, expected, rel_tol=0, abs_tol=0.001)
    return type(actual) is type(expected) and actual == expected


def _expected_annotation(action):
    x, y = action["x"], action["y"]
    if action["type"] == "note":
        return {"subtype": "Text", "contents": action["text"], "rect": [x, y, x + 24, y + 24]}
    rect = [x, y, x + action["width"], y + action["height"]]
    if action["type"] == "highlight":
        return {"subtype": "Highlight", "rect": rect, "color": action["color"]}
    return {"subtype": "Link", "rect": rect, "action_uri": action["url"]}


def _verify_annotations(before, after, actions):
    for original, saved in zip(before, after, strict=True):
        page_actions = [action for action in actions if action["page"] == original["page"]]
        removed = {action["index"] for action in page_actions if action["type"] == "remove"}
        expected = [item for item in original["annotations"] if item["index"] not in removed]
        expected.extend(
            _expected_annotation(action) for action in page_actions if action["type"] != "remove"
        )
        if len(saved["annotations"]) != len(expected) or any(
            not _metadata_matches(actual, wanted)
            for actual, wanted in zip(saved["annotations"], expected, strict=False)
        ):
            raise JobError(
                "PDF Oxide saved annotations differ from requested edits or retained metadata; "
                "0.3.78 may fail to persist removals. No output kept"
            )


def _page_text(doc, count):
    from .oxide_runtime import native

    texts, characters = [], 0
    for index in range(count):
        text = native(
            "annotation source text check",
            lambda i=index: doc.extract_text(i, extract_tables=False, include_artifacts=True),
        )
        characters += len(text)
        if characters > 500_000:
            raise JobError("PDF Oxide annotation text preservation check exceeds its limit")
        texts.append(text)
    return texts


def _check_field_value(field, value):
    if field["is_readonly"]:
        raise JobError("PDF Oxide refuses to modify a read-only form field")
    kind, flags = field["field_type"], field["flags"] or 0
    if kind not in {"text", "choice", "button"}:
        raise JobError("PDF Oxide cannot fill signature or unknown form fields")
    if kind == "button" and flags & (1 << 16):
        raise JobError("PDF Oxide cannot fill push-button action fields")
    if kind == "button" and flags & (1 << 15):
        raise JobError("PDF Oxide 0.3.78 Python cannot safely encode radio-button name values")
    if kind == "text" and value is not None and not isinstance(value, str):
        raise JobError("Text fields require string or null values")
    if kind == "button" and type(value) is not bool:
        raise JobError("Checkbox fields require boolean values")
    if kind == "choice" and value is not None and not isinstance(value, (str, list)):
        raise JobError("Choice fields require a string, list of strings or null")
    if field["max_length"] and isinstance(value, str) and len(value) > field["max_length"]:
        raise JobError("Form value exceeds the field's maximum length")


def _check_native_fill_contract(data, values):
    """Independently validate choices/export states missing from the pinned native field API."""
    import pymupdf

    seen = set()
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            for widget in page.widgets() or ():
                if widget.field_name not in values:
                    continue
                seen.add(widget.field_name)
                value = values[widget.field_name]
                if widget.field_type == pymupdf.PDF_WIDGET_TYPE_CHECKBOX:
                    states = widget.button_states() or {}
                    appearance_kind, appearance = doc.xref_get_key(widget.xref, "AP/N/Yes")
                    if (
                        "Yes" not in states.get("normal", [])
                        or appearance_kind != "xref"
                        or not doc.xref_is_stream(int(appearance.split()[0]))
                    ):
                        raise JobError(
                            "PDF Oxide checkbox filling supports only the native /Yes export state"
                        )
                if (
                    widget.field_type
                    in {pymupdf.PDF_WIDGET_TYPE_COMBOBOX, pymupdf.PDF_WIDGET_TYPE_LISTBOX}
                    and value is not None
                ):
                    choices = widget.choice_values or []
                    allowed = set()
                    for choice in choices:
                        if isinstance(choice, str):
                            allowed.add(choice)
                        elif isinstance(choice, list | tuple) and len(choice) == 2:
                            allowed.add(choice[0])
                        else:
                            raise JobError("Unsupported form choice representation")
                    requested = value if isinstance(value, list) else [value]
                    if isinstance(value, list) and not widget.field_flags & (1 << 21):
                        raise JobError(
                            "A multi-value form update requires a multi-select choice field"
                        )
                    if not widget.field_flags & (1 << 18) and any(
                        v not in allowed for v in requested
                    ):
                        raise JobError("Requested value is not an allowed form choice")
    if seen != values.keys():
        raise JobError("Independent form inspection could not locate every requested field widget")


def _export_preflight(fields, export_format):
    names = [field["name"] for field in fields]
    if len(set(names)) != len(names):
        raise JobError("Duplicate form field names cannot be exported unambiguously")
    for field in fields:
        value = field["value"]
        strings = [field["name"], *(value if isinstance(value, list) else [value])]
        strings = [item for item in strings if isinstance(item, str)]
        if export_format == "fdf":
            if any(not item.isascii() for item in strings):
                raise JobError(
                    "PDF Oxide 0.3.78 FDF Unicode encoding is unsupported; use XFDF or JSON"
                )
            if (
                field["field_type"] == "button"
                and isinstance(value, str)
                and any(char.isspace() or char in "()<>[]{}/%#" for char in value)
            ):
                raise JobError("PDF Oxide FDF cannot safely encode this button export name")
        else:
            if isinstance(value, list) or value == "":
                raise JobError(
                    "PDF Oxide 0.3.78 XFDF loses array/empty-string distinctions; "
                    "use FDF or the exact JSON field report"
                )
            if any(ord(char) < 32 for char in field["name"]):
                raise JobError("PDF Oxide XFDF cannot preserve control characters in field names")
            if any(
                char == "\r"
                or not (
                    char in "\t\n"
                    or 0x20 <= ord(char) <= 0xD7FF
                    or 0xE000 <= ord(char) <= 0xFFFD
                    or 0x10000 <= ord(char) <= 0x10FFFF
                )
                for item in strings
                for char in item
            ):
                raise JobError("PDF Oxide XFDF cannot preserve these XML control characters")


def _verify_xfdf(data, fields):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise JobError("PDF Oxide returned unsupported XML declarations")
    try:
        # Native-generated bounded XML; reject all DTD/entity declarations before parsing.
        root = ElementTree.fromstring(data)  # noqa: S314
    except ElementTree.ParseError as exc:
        raise JobError("PDF Oxide returned invalid XFDF") from exc
    actual = {}
    namespace = "{http://ns.adobe.com/xfdf/}"
    if root.tag != namespace + "xfdf":
        raise JobError("PDF Oxide returned an invalid XFDF root")
    for field in root.findall(f"{namespace}fields/{namespace}field"):
        name = field.attrib.get("name")
        if name in actual:
            raise JobError("PDF Oxide exported duplicate XFDF field names")
        values = field.findall(namespace + "value")
        if len(values) > 1:
            raise JobError("Unexpected PDF Oxide XFDF value representation")
        actual[name] = values[0].text if values else None
    expected = {
        field["name"]: ("Yes" if field["value"] else "Off")
        if type(field["value"]) is bool
        else field["value"]
        for field in fields
    }
    if actual != expected:
        raise JobError("PDF Oxide XFDF values differ from extracted source fields")


def _fill(doc, options, before, count, source_bytes):
    from .oxide_runtime import native

    values = _validate_values(options["values"])
    lookup = {}
    for field in before:
        if field["name"] in lookup:
            raise JobError("Ambiguous duplicate form field names cannot be filled safely")
        lookup[field["name"]] = field
    if values.keys() - lookup.keys():
        raise JobError("A requested form field does not exist")
    _check_native_fill_contract(source_bytes, values)
    for name, value in values.items():
        _check_field_value(lookup[name], value)
        native("setting form value", lambda n=name, v=value: doc.set_form_field_value(n, v))
        if (
            native("checking pending form value", lambda n=name: doc.get_form_field_value(n))
            != value
        ):
            raise JobError("PDF Oxide pending form value differs from the requested value")
    data = native("saving filled PDF", doc.to_bytes)
    saved = _reopen(data, count)
    after = _fields(saved)
    actual = {field["name"]: field["value"] for field in after}
    expected = {name: values.get(name, field["value"]) for name, field in lookup.items()}
    if actual != expected:
        raise JobError("PDF Oxide saved form values differ from requested or untouched values")
    return (
        data,
        saved,
        {
            "fields_before": before,
            "requested_values": values,
            "fields_after": after,
            "saved_values_match": True,
        },
    )


def _inherited_pdf_value(page, key, default):
    node, seen = page.obj, set()
    for _ in range(64):
        if key in node:
            return node[key]
        identity = node.objgen
        if identity in seen or "/Parent" not in node:
            return default
        seen.add(identity)
        node = node["/Parent"]
    raise JobError("Highlight correction page inheritance exceeds its limit")


def _page_snapshots(pdf):
    """Compare geometry and decoded content without mutating page coordinate systems."""
    import pikepdf

    snapshots, decoded_bytes = [], 0
    for page in pdf.pages:
        media, crop = list(map(float, page.mediabox)), list(map(float, page.cropbox))
        rotation = float(_inherited_pdf_value(page, "/Rotate", 0))
        unit = float(page.obj.get("/UserUnit", 1))
        if (
            len(media) != 4
            or len(crop) != 4
            or not all(math.isfinite(value) for value in [*media, *crop, rotation, unit])
        ):
            raise JobError("Highlight correction requires finite page geometry")
        contents = page.obj.get("/Contents", [])
        if isinstance(contents, pikepdf.Stream):
            contents = [contents]
        hashes = []
        for content in contents:
            data = content.read_bytes()
            decoded_bytes += len(data)
            if decoded_bytes > 16 * 1024**2:
                raise JobError("Highlight correction decoded page-content limit exceeded")
            hashes.append(hashlib.sha256(data).hexdigest())
        snapshots.append(
            {
                "media_box": media,
                "crop_box": crop,
                "rotation": rotation,
                "user_unit": unit,
                "decoded_content_sha256": hashes,
            }
        )
    # Identical page commands can render differently if their resources changed.
    resources = _pdf_graph_fingerprints(
        pdf,
        [
            pikepdf.Dictionary(
                Resources=_inherited_pdf_value(page, "/Resources", pikepdf.Dictionary()),
                Group=page.obj.get("/Group", pikepdf.Dictionary()),
            )
            for page in pdf.pages
        ],
        budget={"nodes": 0, "decoded_bytes": decoded_bytes},
    )
    for snapshot, resource in zip(snapshots, resources, strict=True):
        snapshot["inherited_resources_and_page_group_sha256"] = hashlib.sha256(
            json.dumps(resource, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    return snapshots


def _pdf_graph_matches(actual, expected):
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and actual.keys() == expected.keys()
            and all(_pdf_graph_matches(actual[key], value) for key, value in expected.items())
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_pdf_graph_matches(a, b) for a, b in zip(actual, expected, strict=True))
        )
    return _metadata_matches(actual, expected)


def _pdf_graph_fingerprints(pdf, values, *, budget=None):
    """Bounded saved-object graphs independent of native parsing/object numbers."""
    import decimal

    import pikepdf

    pages = {page.obj.objgen: index for index, page in enumerate(pdf.pages)}
    budget = budget if budget is not None else {"nodes": 0, "decoded_bytes": 0}

    def describe(value, seen, depth=0):
        budget["nodes"] += 1
        if depth > 64 or budget["nodes"] > 100_000:
            raise JobError("PDF preservation graph exceeds its validation limit")
        if value is None or type(value) in {bool, int}:
            return value
        if isinstance(value, float | decimal.Decimal):
            number = float(value)
            if not math.isfinite(number):
                raise JobError("PDF preservation requires finite PDF numbers")
            return number
        if isinstance(value, pikepdf.Name):
            return {"name": str(value)}
        if isinstance(value, pikepdf.String):
            return {"string_bytes": bytes(value).hex()}
        identity = value.objgen
        if identity in pages:
            return {"page_reference": pages[identity]}
        marker = None
        if identity != (0, 0):
            if identity in seen:
                return {"reference": seen[identity]}
            marker = len(seen)
            seen[identity] = marker
        if isinstance(value, pikepdf.Array):
            content = [describe(item, seen, depth + 1) for item in value]
        elif isinstance(value, pikepdf.Dictionary | pikepdf.Stream):
            stream = isinstance(value, pikepdf.Stream)
            content = {
                key: describe(item, seen, depth + 1)
                for key, item in sorted(value.items())
                if not (stream and key in {"/Length", "/Filter", "/DecodeParms"})
            }
            if stream:
                decoded = value.read_bytes()
                budget["decoded_bytes"] += len(decoded)
                if budget["decoded_bytes"] > 16 * 1024**2:
                    raise JobError("PDF preservation decoded-stream limit exceeded")
                content["decoded_stream_sha256"] = hashlib.sha256(decoded).hexdigest()
        else:
            raise JobError("Unsupported PDF object in preservation check")
        return {"object": marker, "content": content}

    return [describe(value, {}) for value in values]


def _annotation_fingerprints(pdf, *, exclude_widgets=False, original_counts=None):
    import pikepdf

    values = []
    for index, page in enumerate(pdf.pages):
        raw_annotations = page.obj.get("/Annots", [])
        if original_counts is not None:
            raw_annotations = list(raw_annotations)[: original_counts[index]]
        annotations = [
            annotation
            for annotation in raw_annotations
            if not (exclude_widgets and annotation.get("/Subtype") == pikepdf.Name.Widget)
        ]
        values.append(pikepdf.Array(annotations))
    return _pdf_graph_fingerprints(pdf, values)


def _flatten_targets(page, forms):
    import pikepdf

    return [
        annotation
        for annotation in page.obj.get("/Annots", [])
        if not forms or annotation.get("/Subtype") == pikepdf.Name.Widget
    ]


def _normal_appearance(annotation):
    import pikepdf

    appearance = annotation.get("/AP", pikepdf.Dictionary()).get("/N")
    if isinstance(appearance, pikepdf.Dictionary):
        state = annotation.get("/AS")
        if not isinstance(state, pikepdf.Name) or str(state) not in appearance:
            raise JobError("Flattening requires an explicit, valid normal appearance state")
        appearance = appearance[str(state)]
    return appearance


def _nonpainting_annotation(annotation):
    import pikepdf

    if annotation.get("/Subtype") == pikepdf.Name.Link:
        border, style = annotation.get("/Border"), annotation.get("/BS")
        explicit_zero = (isinstance(style, pikepdf.Dictionary) and style.get("/W") == 0) or (
            isinstance(border, pikepdf.Array) and len(border) in {3, 4} and border[2] == 0
        )
        return (
            explicit_zero
            and (
                border is None
                or (isinstance(border, pikepdf.Array) and len(border) in {3, 4} and border[2] == 0)
            )
            and (style is None or (isinstance(style, pikepdf.Dictionary) and style.get("/W") == 0))
        )
    if annotation.get("/Subtype") == pikepdf.Name.Popup:
        parent = annotation.get("/Parent")
        return isinstance(parent, pikepdf.Dictionary) and isinstance(
            _normal_appearance(parent), pikepdf.Stream
        )
    return False


def _check_flatten_content_state(streams):
    """Require original commands to restore graphics/text state before the native overlay."""
    import pikepdf

    stack, in_text, marked, compatibility, total, commands = [], False, 0, 0, 0, 0
    for stream in streams:
        total += len(stream.read_bytes())
        if total > 16 * 1024**2:
            raise JobError("Flatten preflight content exceeds its decoded-stream limit")
        for instruction in pikepdf.parse_content_stream(stream):
            commands += 1
            if commands > 100_000:
                raise JobError("Flatten preflight content exceeds its command limit")
            operator = str(instruction.operator)
            if operator == "q":
                stack.append((in_text, marked, compatibility))
                if len(stack) > 64 or instruction.operands:
                    raise JobError("Flatten preflight requires bounded valid graphics state")
            elif operator == "Q":
                if (
                    not stack
                    or stack.pop() != (in_text, marked, compatibility)
                    or instruction.operands
                ):
                    raise JobError("Flatten preflight requires balanced graphics/text state")
            elif not stack:
                raise JobError(
                    "Flatten preflight requires original content inside balanced q/Q blocks; "
                    "inherited graphics or clipping state is unsupported"
                )
            elif operator == "BT":
                if in_text:
                    raise JobError("Flatten preflight rejects nested text objects")
                in_text = True
            elif operator == "ET":
                if not in_text:
                    raise JobError("Flatten preflight rejects unbalanced text objects")
                in_text = False
            elif operator in {"BMC", "BDC"}:
                marked += 1
            elif operator == "EMC":
                marked -= 1
                if marked < 0:
                    raise JobError("Flatten preflight requires balanced marked content")
            elif operator == "BX":
                compatibility += 1
            elif operator == "EX":
                compatibility -= 1
                if compatibility < 0:
                    raise JobError("Flatten preflight requires balanced compatibility sections")
    if stack or in_text or marked or compatibility:
        raise JobError("Flatten preflight requires balanced graphics/text state")


def _flatten_source_preflight(source_bytes, indices, *, forms=False):
    """Inspect raw page dictionaries without materializing inherited attributes."""
    import pikepdf

    checked = []
    try:
        with pikepdf.open(io.BytesIO(source_bytes), inherit_page_attributes=False) as source:
            for index in indices:
                page = source.pages[index]
                targets = _flatten_targets(page, forms)
                if not targets:
                    continue
                contents = page.obj.get("/Contents")
                streams = [contents] if isinstance(contents, pikepdf.Stream) else contents
                if (
                    not isinstance(streams, list | pikepdf.Array)
                    or not streams
                    or not all(isinstance(stream, pikepdf.Stream) for stream in streams)
                ):
                    raise JobError(
                        f"PDF Oxide flatten preflight: page {index + 1} requires local content "
                        "streams; the native engine can discard appearances without /Contents"
                    )
                resources = page.obj.get("/Resources")
                if resources is None and _inherited_pdf_value(page, "/Resources", None) is not None:
                    raise JobError(
                        "PDF Oxide flatten preflight: inherited page resources "
                        f"on page {index + 1} "
                        "are unsupported; native flattening can hide inherited fonts/images"
                    )
                if resources is not None and not isinstance(resources, pikepdf.Dictionary):
                    raise JobError("PDF Oxide flatten preflight requires a resource dictionary")
                for annotation in targets:
                    appearance = _normal_appearance(annotation)
                    if annotation.get("/Subtype") != pikepdf.Name.Popup:
                        flags = annotation.get("/F", 0)
                        # Print/read-only/locked flags do not change static visible appearance.
                        if type(flags) is not int or flags < 0 or flags & ~(4 | 64 | 128 | 512):
                            raise JobError(
                                "Flatten preflight rejects appearance-affecting annotation flags"
                            )
                        if (
                            annotation.get("/CA", 1) != 1
                            or annotation.get("/OC") is not None
                            or annotation.get("/BM") not in {None, pikepdf.Name.Normal}
                        ):
                            raise JobError(
                                "Flatten preflight rejects external annotation opacity, blend "
                                "or optional-content membership"
                            )
                    if (
                        appearance is None
                        and annotation.get("/Subtype") == pikepdf.Name.Link
                        and not _nonpainting_annotation(annotation)
                    ):
                        raise JobError(
                            "Flatten preflight requires a saved appearance for links with "
                            "visible or unspecified borders"
                        )
                    if isinstance(appearance, pikepdf.Stream) and list(
                        map(float, appearance.get("/Matrix", [1, 0, 0, 1, 0, 0]))
                    ) != [1, 0, 0, 1, 0, 0]:
                        raise JobError(
                            "PDF Oxide flatten preflight: nonidentity appearance matrices "
                            "are unsupported; native placement cannot be verified"
                        )
                _check_flatten_content_state(streams)
                checked.append(index + 1)
    except JobError:
        raise
    except Exception as exc:
        raise JobError(f"Independent flatten preflight failed ({type(exc).__name__})") from None
    return {
        "engine": "pikepdf",
        "inherit_page_attributes": False,
        "pages_with_flatten_targets": checked,
        "stage": "source preflight before native flattening",
        "local_content_streams_present": True,
        "inherited_resources_shadowing_rejected": True,
        "nonidentity_appearance_matrices_rejected": True,
        "original_graphics_and_text_state_restored": True,
        "external_annotation_appearance_semantics_rejected": True,
    }


def _resource_entries(page):
    import pikepdf

    entries = {(None, "/Group"): page.obj.get("/Group")}
    resources = _inherited_pdf_value(page, "/Resources", pikepdf.Dictionary())
    for category, value in resources.items():
        if isinstance(value, pikepdf.Dictionary):
            entries.update(((category, name), item) for name, item in value.items())
        else:
            entries[(category, None)] = value
    return entries


def _appearance_fingerprint(pdf, appearance, budget):
    import pikepdf

    if not isinstance(appearance, pikepdf.Stream):
        raise JobError("Flattening requires a usable normal appearance for each painted annotation")
    if appearance.get("/Subtype") != pikepdf.Name.Form or appearance.get("/FormType", 1) != 1:
        raise JobError("Flattening requires a normal Form XObject appearance")
    data = appearance.read_bytes()
    budget["decoded_bytes"] += len(data)
    if budget["decoded_bytes"] > 16 * 1024**2:
        raise JobError("Flatten appearance validation exceeds its decoded-stream limit")
    rendering = pikepdf.Dictionary(
        {
            key: value
            for key, value in appearance.items()
            if key
            not in {
                "/Length",
                "/Filter",
                "/DecodeParms",
                "/Type",
                "/FormType",
                "/Resources",
                "/Group",
            }
        }
    )
    rendering["/Matrix"] = appearance.get("/Matrix", pikepdf.Array([1, 0, 0, 1, 0, 0]))
    rendering["/DecodedContentSHA256"] = hashlib.sha256(data).hexdigest()
    return {
        "metadata": _pdf_graph_fingerprints(pdf, [rendering], budget=budget)[0],
        "resources_and_group": _pdf_graph_fingerprints(
            pdf,
            [appearance.get("/Resources", pikepdf.Dictionary()), appearance.get("/Group")],
            budget=budget,
        ),
    }


def _verify_selected_flatten(source, saved, index, before, after, forms, budget):
    import pikepdf

    original_page, page = source.pages[index], saved.pages[index]
    prefix = before["decoded_content_sha256"]
    if after["decoded_content_sha256"][: len(prefix)] != prefix:
        raise JobError("Flattening changed selected-page original content streams; no output kept")
    original_resources, actual_resources = _resource_entries(original_page), _resource_entries(page)
    if not original_resources.keys() <= actual_resources.keys():
        raise JobError("Flattening removed selected-page original resources; no output kept")
    for key, value in original_resources.items():
        original = _pdf_graph_fingerprints(source, [value], budget=budget)[0]
        actual = _pdf_graph_fingerprints(saved, [actual_resources[key]], budget=budget)[0]
        if actual != original:
            raise JobError("Flattening changed selected-page original resources; no output kept")

    appearances = []
    for annotation in _flatten_targets(original_page, forms):
        appearance = _normal_appearance(annotation)
        if appearance is None and _nonpainting_annotation(annotation):
            continue
        fingerprint = _appearance_fingerprint(source, appearance, budget)
        bbox, rect = (
            list(map(float, obj))
            for obj in (appearance.get("/BBox", []), annotation.get("/Rect", []))
        )
        if (
            len(bbox) != 4
            or len(rect) != 4
            or not all(math.isfinite(value) for value in bbox + rect)
            or bbox[2] <= bbox[0]
            or bbox[3] <= bbox[1]
            or rect[2] <= rect[0]
            or rect[3] <= rect[1]
        ):
            raise JobError("Flattening requires finite, nonempty appearance bounds")
        sx, sy = (
            (rect[2] - rect[0]) / (bbox[2] - bbox[0]),
            (rect[3] - rect[1]) / (bbox[3] - bbox[1]),
        )
        appearances.append(
            (fingerprint, [sx, 0, 0, sy, rect[0] - bbox[0] * sx, rect[1] - bbox[1] * sy])
        )

    contents = page.obj.get("/Contents", [])
    if isinstance(contents, pikepdf.Stream):
        contents = [contents]
    instructions = []
    for stream in list(contents)[len(prefix) :]:
        instructions.extend(pikepdf.parse_content_stream(stream))
        if len(instructions) > MAX_ANNOTATIONS * 6:
            raise JobError("Flatten appearance invocation validation exceeds its command limit")
    cursor = 0
    resources = _inherited_pdf_value(page, "/Resources", pikepdf.Dictionary())
    xobjects = resources.get("/XObject", pikepdf.Dictionary())
    for fingerprint, matrix in appearances:

        def take(operator, operands=None):
            nonlocal cursor
            if cursor >= len(instructions):
                raise JobError("Flattened appearance is not invoked by saved page content")
            instruction = instructions[cursor]
            cursor += 1
            if str(instruction.operator) != operator or (
                operands is not None and not _metadata_matches(list(instruction.operands), operands)
            ):
                raise JobError("Flattened appearance invocation/placement differs; no output kept")
            return list(instruction.operands)

        take("q", [])
        # qpdf represents fractional operands as Decimal; compare their numeric values explicitly.
        actual_matrix = take("cm")
        if not _metadata_matches(list(map(float, actual_matrix)), matrix):
            raise JobError("Flattened appearance placement differs; no output kept")
        if cursor < len(instructions) and str(instructions[cursor].operator) == "cm":
            identity = take("cm")
            if list(map(float, identity)) != [1, 0, 0, 1, 0, 0]:
                raise JobError("Flattened appearance applies an unsupported extra transform")
        name = take("Do")
        if len(name) != 1 or not isinstance(name[0], pikepdf.Name) or str(name[0]) not in xobjects:
            raise JobError("Flattened appearance has no valid saved XObject reference")
        actual = _appearance_fingerprint(saved, xobjects[str(name[0])], budget)
        if (
            not _pdf_graph_matches(actual["metadata"], fingerprint["metadata"])
            or actual["resources_and_group"] != fingerprint["resources_and_group"]
        ):
            raise JobError("Flattened appearance content/resources changed; no output kept")
        take("Q", [])
    if cursor != len(instructions):
        raise JobError("Flattening appended unverified selected-page content; no output kept")
    return len(appearances)


def _verify_flatten_saved(source_bytes, data, indices, *, forms=False):
    """Do not equate a native flatten mark with persisted removal or preservation."""
    import pikepdf

    try:
        with (
            pikepdf.open(io.BytesIO(source_bytes), inherit_page_attributes=False) as source,
            pikepdf.open(io.BytesIO(data), inherit_page_attributes=False) as saved,
        ):
            before, after = _page_snapshots(source), _page_snapshots(saved)
            original_annotations = _annotation_fingerprints(source)
            saved_annotations = _annotation_fingerprints(saved)
            if forms:
                original_nonwidgets = _annotation_fingerprints(source, exclude_widgets=True)
                saved_nonwidgets = _annotation_fingerprints(saved, exclude_widgets=True)
            if len(before) != len(after):
                raise JobError("Flattening changed page count; no output kept")
            counts, appearance_counts = [], {}
            selected_budget = {"nodes": 0, "decoded_bytes": 0}
            for index, page in enumerate(saved.pages):
                annotations = page.obj.get("/Annots", [])
                count = sum(
                    not forms or annotation.get("/Subtype") == pikepdf.Name.Widget
                    for annotation in annotations
                )
                counts.append(count)
                if index in indices:
                    if count:
                        raise JobError(
                            f"PDF Oxide flatten failed: {count} "
                            f"{'widget ' if forms else ''}annotations remain on page {index + 1}; "
                            "no output kept"
                        )
                    if forms and not _pdf_graph_matches(
                        saved_nonwidgets[index], original_nonwidgets[index]
                    ):
                        raise JobError(
                            "Form flatten changed non-widget annotations; no output kept"
                        )
                    if any(
                        before[index][key] != after[index][key]
                        for key in ("media_box", "crop_box", "rotation", "user_unit")
                    ):
                        raise JobError("Flattening changed page geometry; no output kept")
                    appearance_counts[index + 1] = _verify_selected_flatten(
                        source, saved, index, before[index], after[index], forms, selected_budget
                    )
                elif before[index] != after[index] or not _pdf_graph_matches(
                    saved_annotations[index], original_annotations[index]
                ):
                    raise JobError("Flattening changed an unselected page; no output kept")
    except JobError:
        raise
    except Exception as exc:
        raise JobError(f"Independent flatten verification failed ({type(exc).__name__})") from None
    return {
        "engine": "pikepdf",
        "selected_pages_have_no_widgets" if forms else "selected_pages_have_no_annotations": True,
        "unselected_page_geometry_content_resources_annotations_unchanged": True,
        "selected_page_original_content_resources_preserved": True,
        "selected_appearances_attached_and_invoked": True,
        "selected_appearance_counts": appearance_counts,
        "annotation_numeric_tolerance_pdf_points": 0.001,
        "saved_widget_counts" if forms else "saved_annotation_counts": counts,
        "visual_accuracy_verified": False,
    }


def _repair_native_highlights(source_bytes, native_bytes, actions, before):
    """Explicit pikepdf correction for the measured native QuadPoints/no-AP defect only."""
    import pikepdf

    if pikepdf.__version__ != "10.17.0":
        raise JobError("Highlight correction requires pinned pikepdf 10.17.0")
    with (
        pikepdf.open(io.BytesIO(source_bytes)) as source,
        pikepdf.open(io.BytesIO(native_bytes)) as pdf,
    ):
        source_pages, native_pages = _page_snapshots(source), _page_snapshots(pdf)
        if source_pages != native_pages:
            raise JobError(
                "Native annotation editing changed page geometry/content; correction refused"
            )
        original_counts = [len(page.obj.get("/Annots", [])) for page in source.pages]
        original_annotations = _annotation_fingerprints(source)
        if not _pdf_graph_matches(
            _annotation_fingerprints(pdf, original_counts=original_counts), original_annotations
        ):
            raise JobError("Native editing changed original annotations; correction refused")
        corrections = []
        for page_number, page in enumerate(pdf.pages, 1):
            page_actions = [action for action in actions if action["page"] == page_number]
            added = [action for action in page_actions if action["type"] != "remove"]
            if not any(action["type"] == "highlight" for action in added):
                continue
            if any(action["type"] == "remove" for action in page_actions):
                raise JobError("Highlight correction cannot be combined with annotation removal")
            original_count = len(before[page_number - 1]["annotations"])
            annotations = page.obj.get("/Annots", [])
            if len(annotations) != original_count + len(added):
                raise JobError("Native annotation identities are ambiguous; correction refused")
            layout = native_pages[page_number - 1]
            if layout["rotation"] not in {0, 90, 180, 270} or layout["user_unit"] != 1:
                raise JobError("Highlight correction requires right-angle rotation and UserUnit 1")
            media, crop = layout["media_box"], layout["crop_box"]
            visible = [
                max(media[0], crop[0]),
                max(media[1], crop[1]),
                min(media[2], crop[2]),
                min(media[3], crop[3]),
            ]
            for offset, action in enumerate(added):
                if action["type"] != "highlight":
                    continue
                index = original_count + offset
                annotation = annotations[index]
                x, y, width, height = (action[k] for k in ("x", "y", "width", "height"))
                rect = [x, y, x + width, y + height]
                if not (
                    visible[0] <= x < x + width <= visible[2]
                    and visible[1] <= y < y + height <= visible[3]
                ):
                    raise JobError("Corrected highlight must fit fully inside the visible crop")
                old_rect = list(map(float, annotation.get("/Rect", [])))
                old_quad = list(map(float, annotation.get("/QuadPoints", [])))
                expected_bad_quad = [x, y, x + width, y, x + width, y + height, x, y + height]
                if (
                    annotation.get("/Subtype") != pikepdf.Name.Highlight
                    or not _metadata_matches(old_rect, rect)
                    or not _metadata_matches(old_quad, expected_bad_quad)
                    or not _metadata_matches(
                        list(map(float, annotation.get("/C", []))), action["color"]
                    )
                    or annotation.get("/AP") is not None
                ):
                    raise JobError(
                        "Highlight differs from the known native defect; correction refused"
                    )
                quad = [x, y + height, x + width, y + height, x, y, x + width, y]
                numbers = [*action["color"], width, height]
                red, green, blue, wide, high = [
                    format(n, ".10f").rstrip("0").rstrip(".") or "0" for n in numbers
                ]
                appearance_bytes = (
                    f"q /HighlightRepair gs {red} {green} {blue} rg 0 0 {wide} {high} re f Q\n"
                ).encode("ascii")
                appearance = pikepdf.Stream(pdf, appearance_bytes)
                appearance["/Type"] = pikepdf.Name.XObject
                appearance["/Subtype"] = pikepdf.Name.Form
                appearance["/FormType"] = 1
                appearance["/BBox"] = pikepdf.Array([0, 0, width, height])
                appearance["/Matrix"] = pikepdf.Array([1, 0, 0, 1, 0, 0])
                appearance["/Resources"] = pikepdf.Dictionary(
                    ExtGState=pikepdf.Dictionary(
                        HighlightRepair=pikepdf.Dictionary(
                            Type=pikepdf.Name.ExtGState, BM=pikepdf.Name.Multiply, ca=1, CA=1
                        )
                    )
                )
                annotation["/QuadPoints"] = pikepdf.Array(quad)
                annotation["/Rect"] = pikepdf.Array(rect)
                annotation["/AP"] = pikepdf.Dictionary(N=appearance)
                corrections.append(
                    {
                        "page": page_number,
                        "annotation_index": index,
                        "requested_rectangle": rect,
                        "native_rectangle": old_rect,
                        "native_quadpoints": old_quad,
                        "corrected_quadpoints": quad,
                        "native_appearance": None,
                        "appearance_bbox": [0, 0, width, height],
                        "appearance_sha256": hashlib.sha256(appearance_bytes).hexdigest(),
                        "blend_mode": "Multiply",
                        "opacity": 1,
                        "page_geometry": layout,
                    }
                )
        if not corrections:
            raise JobError("No known native highlights were found for explicit correction")
        output = io.BytesIO()
        pdf.save(output)
        corrected = output.getvalue()
    if len(corrected) > 16 * 1024**2:
        raise JobError("Corrected highlight PDF exceeds its output limit")
    with pikepdf.open(io.BytesIO(corrected)) as saved:
        if _page_snapshots(saved) != source_pages:
            raise JobError("Highlight correction changed page geometry/content; no output kept")
        if not _pdf_graph_matches(
            _annotation_fingerprints(saved, original_counts=original_counts), original_annotations
        ):
            raise JobError("Highlight correction changed original annotations; no output kept")
        for record in corrections:
            annotation = saved.pages[record["page"] - 1].obj.Annots[record["annotation_index"]]
            if (
                list(map(float, annotation.QuadPoints)) != record["corrected_quadpoints"]
                or hashlib.sha256(annotation.AP.N.read_bytes()).hexdigest()
                != record["appearance_sha256"]
            ):
                raise JobError("Corrected highlight appearance did not persist; no output kept")
    return corrected, {
        "engine": "pikepdf",
        "version": pikepdf.__version__,
        "explicit_opt_in": True,
        "native_defect_fixed_upstream": False,
        "native_comparison_file": "native-unverified.pdf",
        "native_sha256": hashlib.sha256(native_bytes).hexdigest(),
        "corrected_sha256": hashlib.sha256(corrected).hexdigest(),
        "source_page_geometry_decoded_content_and_resources_unchanged": True,
        "source_annotation_graphs_unchanged": True,
        "annotation_numeric_tolerance_pdf_points": 0.001,
        "coordinates": "Native PDF coordinates retained; PDF viewers apply CropBox/Rotate",
        "appearance_policy": "Explicit rectangle with Multiply blending; no layout inference",
        "changes": corrections,
    }


def _edit_annotations(doc, options, count, source_bytes):
    from .oxide_runtime import native

    repair = options.get("allow_highlight_repair", False)
    actions = _validate_actions(options["actions"], repair)
    indices = sorted({action["page"] - 1 for action in actions})
    if any(index >= count for index in indices):
        raise JobError("Annotation action page exceeds the document page count")
    before = _annotations(doc, range(count))
    fields_before = _fields(doc) if repair else None
    text_before = _page_text(doc, count)
    for index in indices:
        page = native("annotation page access", lambda i=index: doc.page(i))
        originals = native("editable annotation inspection", page.annotations)
        page_actions = [action for action in actions if action["page"] == index + 1]
        removals = sorted(
            (action["index"] for action in page_actions if action["type"] == "remove"), reverse=True
        )
        for annotation_index in removals:
            if annotation_index >= len(originals):
                raise JobError("Annotation removal index exceeds the original page annotations")
            if originals[annotation_index].subtype == "Widget":
                raise JobError(
                    "Use form operations to modify widgets; annotation removal is refused"
                )
            if (
                native(
                    "annotation removal", lambda n=annotation_index, p=page: p.remove_annotation(n)
                )
                is not True
            ):
                raise JobError("PDF Oxide did not remove the requested annotation")
        for action in page_actions:
            kind = action["type"]
            if kind == "remove":
                continue
            if kind == "note":
                native(
                    "adding note", lambda a=action, p=page: p.add_note(a["x"], a["y"], a["text"])
                )
            elif kind == "highlight":
                native(
                    "adding highlight",
                    lambda a=action, p=page: p.add_highlight(
                        a["x"], a["y"], a["width"], a["height"], tuple(a["color"])
                    ),
                )
            elif kind == "link":
                native(
                    "adding link",
                    lambda a=action, p=page: p.add_link(
                        a["x"], a["y"], a["width"], a["height"], a["url"]
                    ),
                )
        native("saving annotation page", lambda p=page: doc.save_page(p))
    data = native("saving annotation PDF", doc.to_bytes)
    saved = _reopen(data, count)
    after = _annotations(saved, range(count))
    _verify_annotations(before, after, actions)
    if _page_text(saved, count) != text_before:
        raise JobError("PDF Oxide annotation rewrite changed extracted page text; no output kept")
    changes = {}
    native_comparison = None
    if repair:
        native_fields = _fields(saved)
        if not _metadata_matches(native_fields, fields_before):
            raise JobError("Native editing changed source form fields; correction refused")
        native_comparison = data
        try:
            data, correction = _repair_native_highlights(source_bytes, data, actions, before)
        except JobError:
            raise
        except Exception as exc:
            raise JobError(f"pikepdf highlight correction failed ({type(exc).__name__})") from None
        saved = _reopen(data, count)
        after = _annotations(saved, range(count))
        _verify_annotations(before, after, actions)
        if _page_text(saved, count) != text_before or _fields(saved) != native_fields:
            raise JobError("Highlight correction changed text/form values; no output kept")
        changes["highlight_correction"] = correction
    return (
        data,
        saved,
        {
            "actions": actions,
            "pages_before": before,
            "pages_after": after,
            "saved_annotation_metadata_match": True,
            "native_page_text_unchanged": True,
            "visual_accuracy_verified": False,
            **changes,
        },
        native_comparison,
    )


def process(request, options, add):
    from .oxide_runtime import (
        native,
        open_source,
        require_runtime,
        selected,
        write_bytes,
        write_json,
    )

    require_runtime(request)
    operation = request["operation"]
    validate_options(operation, options)
    doc, source_bytes, source = open_source(request, options)
    count = source["pages"]
    if count > MAX_DOCUMENT_PAGES and operation != "oxide_annotation_inspect":
        raise JobError("PDF Oxide form and annotation rewriting scans at most 100 document pages")
    indices = selected(options, count) if OPERATIONS[operation]["pages"] else list(range(count))
    report = {
        "engine": "pdf-oxide",
        "engine_version": "0.3.78",
        "operation": operation,
        "source": source,
        "source_unchanged": True,
        "coordinates": "Native PDF user-space rectangles; no rotation/crop normalization",
        "visual_accuracy_verified": False,
    }
    warnings = []
    data, saved = None, None
    rewriting = operation in {
        "oxide_form_fill",
        "oxide_form_flatten",
        "oxide_annotation_edit",
        "oxide_annotation_flatten",
    }
    if rewriting:
        if native("source permission inspection", doc.permissions) is not None:
            raise JobError(
                "PDF Oxide form/annotation rewriting cannot preserve encrypted input protections; "
                "use an explicitly authorized decryption workflow first"
            )
        if native("source signature count", doc.signature_count):
            warnings.append("Rewriting this PDF invalidates its existing digital signatures")
    if operation.startswith("oxide_form_"):
        fields = _fields(doc)
        has_xfa = native("XFA presence inspection", doc.has_xfa)
        report.update({"fields": fields, "has_xfa": has_xfa})
        if has_xfa:
            if operation != "oxide_form_inspect":
                raise JobError(
                    "PDF Oxide 0.3.78 Python supports XFA detection only; "
                    "this operation is unsupported"
                )
            warnings.append(
                "XFA is present; its data and appearance are not represented by AcroForm fields"
            )
        if operation == "oxide_form_inspect":
            report["pages"] = _annotations(doc, indices)
        elif operation == "oxide_form_fill":
            data, saved, changes = _fill(doc, options, fields, count, source_bytes)
            report.update(changes)
            warnings.append(
                "Saved field values were checked; form appearance and glyph fidelity "
                "require visual review"
            )
        elif operation == "oxide_form_export":
            _export_preflight(fields, options["format"])
            filename = "form-data." + options["format"]
            target = Path(request["workdir"]) / filename
            native(
                "exporting form data", lambda: doc.export_form_data(str(target), options["format"])
            )
            if target.is_symlink() or not target.is_file() or target.stat().st_size > MAX_REPORT:
                raise JobError(
                    "PDF Oxide form-data export exceeds its limit or has an invalid destination"
                )
            exported = target.read_bytes()
            target.unlink()
            if options["format"] == "xfdf":
                _verify_xfdf(exported, fields)
            write_bytes(request, add, filename, exported)
            report["export"] = {
                "filename": filename,
                "format": options["format"],
                "bytes": len(exported),
                "sha256": hashlib.sha256(exported).hexdigest(),
                "appearance_included": False,
            }
        elif operation == "oxide_form_flatten":
            report["pages_before"] = _annotations(doc, indices)
            report["flatten_preflight"] = _flatten_source_preflight(
                source_bytes, indices, forms=True
            )
            if set(indices) == set(range(count)):
                native("flattening all forms", doc.flatten_forms)
            else:
                for index in indices:
                    native("flattening page forms", lambda i=index: doc.flatten_forms_on_page(i))
            data = native("saving flattened forms", doc.to_bytes)
            report["flatten_warnings"] = native("form flatten diagnostics", doc.flatten_warnings)
            warnings.extend(report["flatten_warnings"])
            if report["flatten_warnings"]:
                _bounded_report(report["flatten_warnings"])
                raise JobError(
                    "PDF Oxide reported form-flatten loss; no output kept: "
                    + json.dumps(report["flatten_warnings"], ensure_ascii=False)
                )
            saved = _reopen(data, count)
            report["independent_flatten_verification"] = _verify_flatten_saved(
                source_bytes, data, indices, forms=True
            )
            report["fields_after"] = _fields(saved)
            report["pages_after"] = _annotations(saved, indices)
            warnings.append(
                "Form flattening changes interactivity and may lose appearances; "
                "compare every field with the source"
            )
    elif operation == "oxide_annotation_inspect":
        report["pages"] = _annotations(doc, indices)
    elif operation == "oxide_annotation_edit":
        data, saved, changes, native_comparison = _edit_annotations(
            doc, options, count, source_bytes
        )
        report.update(changes)
        if native_comparison is not None:
            write_bytes(request, add, "native-unverified.pdf", native_comparison)
            warnings.append(
                "pikepdf explicitly corrected new highlight geometry/appearances. "
                "native-unverified.pdf retains PDF Oxide's defective result; use modified.pdf "
                "for the corrected candidate and compare both with the source"
            )
        warnings.append(
            "Annotation edits require visual review for geometry, preserved content "
            "and link destinations"
        )
    elif operation == "oxide_annotation_flatten":
        report["pages_before"] = _annotations(doc, indices)
        if any(
            item.get("subtype") == "Widget"
            for page in report["pages_before"]
            for item in page["annotations"]
        ):
            raise JobError(
                "Annotation flattening on widget-bearing pages is unsupported; "
                "flatten forms separately"
            )
        report["flatten_preflight"] = _flatten_source_preflight(source_bytes, indices)
        if set(indices) == set(range(count)):
            native("flattening all annotations", doc.flatten_all_annotations)
        else:
            for index in indices:
                native(
                    "flattening page annotations", lambda i=index: doc.flatten_page_annotations(i)
                )
        report["marked_pages"] = [
            index + 1
            for index in indices
            if native("checking flatten marks", lambda i=index: doc.is_page_marked_for_flatten(i))
        ]
        data = native("saving flattened annotations", doc.to_bytes)
        saved = _reopen(data, count)
        report["pages_after"] = _annotations(saved, indices)
        if any(page["annotations"] for page in report["pages_after"]):
            raise JobError("PDF Oxide flatten failed: annotations remain; no output kept")
        report["independent_flatten_verification"] = _verify_flatten_saved(
            source_bytes, data, indices
        )
        warnings.append(
            "Flattened annotations can lose content without usable appearance streams; "
            "links are removed"
        )
    else:
        raise JobError("Unsupported PDF Oxide form/annotation operation")
    report["structured_warnings"] = {
        "source": _diagnostics(doc),
        "saved": _diagnostics(saved) if saved is not None else [],
    }
    report["warnings"] = warnings
    _bounded_report(report)
    if data is not None:
        report["output_sha256"] = hashlib.sha256(data).hexdigest()
        write_bytes(request, add, "modified.pdf", data)
    write_json(request, add, "forms-annotations.json", report)
    return warnings, False
