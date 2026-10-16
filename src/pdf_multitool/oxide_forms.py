"""Bounded PDF Oxide form and annotation workflows, preserving the source PDF."""

from __future__ import annotations

import hashlib
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
        "Removal fails closed when the saved annotation remains. New highlights are unsupported "
        "because pinned output disappears in PDFium.",
        pdf=True,
        options={
            "actions": _field(
                "Annotation actions (JSON array)",
                default="[]",
                multiline=True,
                maxLength=MAX_CONFIG,
            )
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


def _validate_actions(text):
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
            raise JobError(
                "PDF Oxide 0.3.78 new highlights are unsupported: "
                "the saved appearance disappears in PDFium"
            )
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
    return actions


def validate_options(operation, options):
    """Called before spawn and again inside the worker; no document/parser access."""
    if operation == "oxide_form_fill":
        _validate_values(options["values"])
    elif operation == "oxide_annotation_edit":
        _validate_actions(options["actions"])
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


def _edit_annotations(doc, options, count):
    from .oxide_runtime import native

    actions = _validate_actions(options["actions"])
    indices = sorted({action["page"] - 1 for action in actions})
    if any(index >= count for index in indices):
        raise JobError("Annotation action page exceeds the document page count")
    before = _annotations(doc, range(count))
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
        },
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
            if set(indices) == set(range(count)):
                native("flattening all forms", doc.flatten_forms)
            else:
                for index in indices:
                    native("flattening page forms", lambda i=index: doc.flatten_forms_on_page(i))
            data = native("saving flattened forms", doc.to_bytes)
            report["flatten_warnings"] = native("form flatten diagnostics", doc.flatten_warnings)
            warnings.extend(report["flatten_warnings"])
            saved = _reopen(data, count)
            report["fields_after"] = _fields(saved)
            report["pages_after"] = _annotations(saved, indices)
            warnings.append(
                "Form flattening changes interactivity and may lose appearances; "
                "compare every field with the source"
            )
    elif operation == "oxide_annotation_inspect":
        report["pages"] = _annotations(doc, indices)
    elif operation == "oxide_annotation_edit":
        data, saved, changes = _edit_annotations(doc, options, count)
        report.update(changes)
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
