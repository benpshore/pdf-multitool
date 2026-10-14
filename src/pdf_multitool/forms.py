"""Bounded AcroForm editing using PyMuPDF only.

The caller owns the document and saving/flattening. All supplied changes are
validated before editing it. Unexpected engine failures can still leave that
in-memory document partially changed: callers must discard it on any failure.
Radio filling changes existing state/appearance references, never synthesizes a
new radio appearance. New fields use Helvetica and single-line WinAnsi text.
"""
from __future__ import annotations

import math
import re
import unicodedata
from pathlib import Path

import pymupdf

from .core import JobError

_MAX_FIELDS = 200
_MAX_WIDGETS = 10000
_MAX_VALUE = 100000
_TYPES = {
    "text": pymupdf.PDF_WIDGET_TYPE_TEXT,
    "checkbox": pymupdf.PDF_WIDGET_TYPE_CHECKBOX,
    "combobox": pymupdf.PDF_WIDGET_TYPE_COMBOBOX,
    "listbox": pymupdf.PDF_WIDGET_TYPE_LISTBOX,
}
_RADIO = pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON
_CHOICE = {_TYPES["combobox"], _TYPES["listbox"]}
_MULTILINE = 1 << 12
_MULTISELECT = 1 << 21
_COMBO = 1 << 17
_UNSUPPORTED_TEXT_FLAGS = (1 << 13) | (1 << 20) | (1 << 25)
_NAME_REF = re.compile(r"([1-9][0-9]*)\s+0\s+R")


def _fail(message):
    raise JobError(message)


def _string(value, what, limit, *, empty=False, multiline=False, visible=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        _fail(f"{what} must be {'a bounded' if empty else 'a nonempty bounded'} string (maximum {limit} characters)")
    if any(unicodedata.category(c).startswith("C") and not (multiline and c == "\n") for c in value):
        _fail(f"{what} contains unsupported control characters")
    if visible:
        try:
            value.encode("cp1252")
        except UnicodeEncodeError:
            _fail(f"{what} has unsupported characters; visible form values currently require Windows-1252 text")
    return value


def _ref(doc, value, what):
    match = _NAME_REF.fullmatch(value.strip())
    if not match or not 0 < int(match[1]) < doc.xref_length():
        _fail(f"Malformed {what} reference")
    number = int(match[1])
    if not doc.xref_object(number).lstrip().startswith("<<") or doc.xref_is_stream(number):
        _fail(f"Malformed {what} object")
    return number


def _refs(doc, kind, value, what):
    if kind != "array" or not value.startswith("[") or not value.endswith("]"):
        _fail(f"Malformed {what} array")
    body = value[1:-1]
    matches = list(_NAME_REF.finditer(body))
    if _NAME_REF.sub("", body).strip() or len(matches) > _MAX_WIDGETS:
        _fail(f"Malformed or excessive {what} array")
    refs = [_ref(doc, match[0], what) for match in matches]
    if not refs or len(refs) != len(set(refs)):
        _fail(f"Empty or duplicate {what} references")
    return refs


def _chain(doc, xref):
    chain, seen = [], set()
    for _ in range(32):
        if xref in seen:
            _fail("Malformed form field parent cycle")
        seen.add(xref)
        chain.append(xref)
        kind, value = doc.xref_get_key(xref, "Parent")
        if kind == "null":
            return chain
        if kind != "xref":
            _fail("Malformed form field parent")
        parent = _ref(doc, value, "form parent")
        kids = _refs(doc, *doc.xref_get_key(parent, "Kids"), "form parent Kids")
        if xref not in kids:
            _fail("Malformed form parent does not contain its child")
        xref = parent
    _fail("Form parent hierarchy exceeds the supported depth")


def _inherited(doc, chain, key):
    for xref in chain:
        kind, value = doc.xref_get_key(xref, key)
        if kind != "null":
            return kind, value
    return "null", "null"


def _flags(doc, chain):
    kind, value = _inherited(doc, chain, "Ff")
    if kind == "null":
        return 0
    if kind != "int":
        _fail("Malformed field flags")
    flags = int(value)
    if not 0 <= flags <= 0x7fffffff:
        _fail("Malformed field flags")
    return flags


def _preflight(doc):
    """Check parent chains without asking MuPDF to inherit malformed fields."""
    native = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    total = 0
    for index in range(len(doc)):
        page = native.pdf_load_object(pdf, doc.page_xref(index))
        annots = native.pdf_dict_gets(page, "Annots")
        if not native.pdf_is_null(annots) and not native.pdf_is_array(annots):
            _fail("Malformed page annotation array")
        for item in range(native.pdf_array_len(annots)):
            obj = native.pdf_array_get(annots, item)
            if native.pdf_to_name(native.pdf_dict_gets(obj, "Subtype")) != "Widget":
                continue
            if not native.pdf_is_indirect(obj):
                _fail("Direct form widget objects are not supported")
            total += 1
            if total > _MAX_WIDGETS:
                _fail("PDF has too many widgets for bounded form editing")
            _chain(doc, native.pdf_to_num(obj))


def _all_field_names(doc):
    """Include named fields without a visible widget when reserving new names."""
    native = pymupdf.mupdf
    catalog = native.pdf_load_object(pymupdf._as_pdf_document(doc), doc.pdf_catalog())
    fields = native.pdf_dict_getp(catalog, "AcroForm/Fields")
    if native.pdf_is_null(fields):
        return set()
    if not native.pdf_is_array(fields):
        _fail("Malformed AcroForm Fields array")
    if native.pdf_array_len(fields) > _MAX_WIDGETS:
        _fail("Excessive AcroForm Fields array")
    stack = [(native.pdf_array_get(fields, index), "", 0) for index in range(native.pdf_array_len(fields))]
    names, seen = set(), set()
    while stack:
        obj, prefix, depth = stack.pop()
        if depth >= 32 or len(seen) >= _MAX_WIDGETS or not native.pdf_is_indirect(obj):
            _fail("Unsupported or excessive AcroForm field hierarchy")
        xref = native.pdf_to_num(obj)
        if xref in seen:
            _fail("Duplicate or cyclic AcroForm field hierarchy")
        seen.add(xref)
        kind, name = doc.xref_get_key(xref, "T")
        if kind not in {"null", "string"}:
            _fail("Malformed form field name")
        if kind == "string":
            prefix = prefix + "." + name if prefix else name
            names.update((name, prefix))
        kids = native.pdf_dict_gets(obj, "Kids")
        if not native.pdf_is_null(kids) and not native.pdf_is_array(kids):
            _fail("Malformed AcroForm Kids array")
        if len(stack) + native.pdf_array_len(kids) + len(seen) > _MAX_WIDGETS:
            _fail("Excessive AcroForm field hierarchy")
        for index in range(native.pdf_array_len(kids)):
            stack.append((native.pdf_array_get(kids, index), prefix, depth + 1))
    return names


def _widgets(doc):
    """Keep the page objects alive for PyMuPDF's weak widget references."""
    _preflight(doc)
    pages, rows = [], []
    for page in doc:
        pages.append(page)
        for widget in page.widgets() or ():
            rows.append((page.number + 1, widget))
            if len(rows) > _MAX_WIDGETS:
                _fail("PDF has too many widgets for bounded form editing")
    return pages, rows


def _normal_states(doc, widget):
    # Use the pinned PyMuPDF/MuPDF dictionary API: Widget.button_states splits
    # serialized names and cannot reliably decode escaped export values.
    native = pymupdf.mupdf
    obj = native.pdf_load_object(pymupdf._as_pdf_document(doc), widget.xref)
    normal = native.pdf_dict_getp(obj, "AP/N")
    if not native.pdf_is_dict(normal) or native.pdf_dict_len(normal) != 2:
        _fail(f"Button needs exactly one export value and an Off appearance: {widget.field_name}")
    states = []
    for index in range(2):
        state = native.pdf_to_name(native.pdf_dict_get_key(normal, index))
        stream = native.pdf_dict_get_val(normal, index)
        if not native.pdf_is_stream(stream):
            _fail(f"Button appearance is not a stream: {widget.field_name}")
        states.append(state)
    if "Off" not in states or len(set(states)) != 2:
        _fail(f"Button needs exactly one export value and an Off appearance: {widget.field_name}")
    selected = next(value for value in states if value != "Off")
    _string(selected, "Button export value", 256)
    return selected


def button_export_value(doc, widget) -> str:
    """Return a decoded export value for a supported existing checkbox/radio.

    Raises JobError for ambiguous or malformed normal appearance dictionaries.
    """
    if widget.field_type not in {_RADIO, _TYPES["checkbox"]}:
        _fail("An export value is only defined for checkbox or radio widgets")
    return _normal_states(doc, widget)


def _pdf_name(value):
    raw = value.encode("utf-8")
    return "/" + "".join(chr(c) if 33 <= c <= 126 and c not in b"()<>[]{}/%#" else f"#{c:02X}" for c in raw)


def _choice_values(widget):
    values = widget.choice_values
    if not isinstance(values, (list, tuple)) or not values or len(values) > 1000:
        _fail(f"Choice field needs bounded explicit choices: {widget.field_name}")
    if any(not isinstance(value, str) for value in values):
        _fail(f"Export/display choice pairs are not supported: {widget.field_name}")
    for value in values:
        _string(value, "Choice", 1024, visible=True)
    if len(values) != len(set(values)):
        _fail(f"Choice field has duplicate ambiguous choices: {widget.field_name}")
    return list(values)


def _radio_plan(doc, name, value, rows):
    _string(value, f"Radio group {name} requires an explicit export value", 256)
    if value == "Off":
        _fail(f"Radio group requires a selected export value, not Off: {name}")
    chains = [_chain(doc, widget.xref) for _, widget in rows]
    if any(len(chain) < 2 for chain in chains):
        _fail(f"Radio group requires a genuine shared parent: {name}")
    parents = {chain[1] for chain in chains}
    if len(parents) != 1:
        _fail(f"Independent or ambiguous radio groups share this name: {name}")
    parent = next(iter(parents))
    kids = _refs(doc, *doc.xref_get_key(parent, "Kids"), "radio Kids")
    if set(kids) != {widget.xref for _, widget in rows} or len(rows) < 2:
        _fail(f"Radio group has missing, mixed, or unsupported children: {name}")
    parent_flags = _flags(doc, chains[0][1:])
    if not parent_flags & (1 << 15) or parent_flags & ((1 << 16) | (1 << 25)):
        _fail(f"Unsupported radio group flags: {name}")
    if parent_flags & 1:
        _fail(f"Form field is read-only: {name}")
    exports, widgets = set(), []
    for (_, widget), chain in zip(rows, chains):
        if widget.field_type != _RADIO or _flags(doc, chain) != parent_flags:
            _fail(f"Mixed radio child types or flags: {name}")
        if doc.xref_get_key(widget.xref, "T")[0] != "null":
            _fail(f"Radio child names are not supported: {name}")
        if doc.xref_get_key(widget.xref, "V")[0] != "null":
            _fail(f"Radio child value overrides are not supported: {name}")
        export = _normal_states(doc, widget)
        if export in exports:
            _fail(f"Radio group has duplicate ambiguous export values: {name}")
        exports.add(export)
        widgets.append((widget.xref, export))
    if value not in exports:
        _fail(f"Unknown radio export value for {name}")
    return parent, widgets, value


def _snapshot(doc):
    pages, rows = _widgets(doc)
    result = []
    for page, widget in rows:
        entry = {"page": page, "name": widget.field_name, "type": widget.field_type,
                 "rect": list(widget.rect), "flags": widget.field_flags,
                 "label": widget.field_label, "max_length": widget.text_maxlen,
                 "choices": widget.choice_values}
        if widget.field_type != pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
            entry["value"] = widget.field_value
        if widget.field_type in {_RADIO, _TYPES["checkbox"]}:
            entry["state"] = list(doc.xref_get_key(widget.xref, "AS"))
        if widget.field_type == _RADIO:
            chain = _chain(doc, widget.xref)
            entry["group_value"] = list(doc.xref_get_key(chain[1], "V")) if len(chain) > 1 else None
        result.append(entry)
    return {"page_count": len(doc), "widgets": result}


def fill_fields(doc, mapping: dict) -> dict:
    """Fill supported existing fields, with explicit radio export values.

    Multi-select and paired export/display choices, password/file/rich-text
    fields, signature fields, duplicate non-radio names, and ambiguous radio
    structures are deliberately unsupported. Values are Windows-1252 text.
    """
    if not isinstance(mapping, dict) or not 1 <= len(mapping) <= _MAX_FIELDS:
        _fail("Form fill needs a nonempty field-name/value object with at most 200 fields")
    for name in mapping:
        _string(name, "Field name", 256)
    pages, rows = _widgets(doc)
    groups = {}
    for page, widget in rows:
        groups.setdefault(widget.field_name, []).append((page, widget))
    missing = set(mapping) - groups.keys()
    if missing:
        _fail("Unknown form fields: " + ", ".join(sorted(missing)))
    plans = []
    for name, value in mapping.items():
        group = groups[name]
        if any(widget.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE for _, widget in group):
            _fail("Signature fields cannot be filled as ordinary form text")
        if any(widget.field_type == _RADIO for _, widget in group):
            plans.append(("radio", _radio_plan(doc, name, value, group)))
            continue
        if len(group) != 1:
            _fail(f"Duplicate or shared non-radio field names are not supported: {name}")
        widget = group[0][1]
        chain = _chain(doc, widget.xref)
        flags = _flags(doc, chain)
        if flags & 1:
            _fail(f"Form field is read-only: {name}")
        kind = widget.field_type
        if kind == _TYPES["checkbox"]:
            if not isinstance(value, bool):
                _fail(f"Checkbox field needs true or false: {name}")
            export = _normal_states(doc, widget)
            # The normal update regenerates the AP while preserving the export.
            plans.append(("ordinary", (widget, export if value else "Off")))
        elif kind in {_TYPES["text"], *_CHOICE}:
            if kind == _TYPES["text"] and flags & _UNSUPPORTED_TEXT_FLAGS:
                _fail(f"Password, file-select, and rich-text fields are not supported: {name}")
            if widget.text_font and widget.text_font.lower() == "zadb":
                _fail(f"Dingbat-font text and choice fields are not supported: {name}")
            _string(value, f"Form value for {name}", _MAX_VALUE, empty=True,
                    multiline=kind == _TYPES["text"] and bool(flags & _MULTILINE), visible=True)
            if kind == _TYPES["text"]:
                mk, mv = _inherited(doc, chain, "MaxLen")
                if mk not in {"null", "int"} or (mk == "int" and int(mv) < 0):
                    _fail(f"Malformed maximum length: {name}")
                maximum = int(mv) if mk == "int" else 0
                if maximum and len(value) > maximum:
                    _fail(f"Form value exceeds the field's maximum length: {name}")
            else:
                if flags & _MULTISELECT:
                    _fail(f"Multi-select choice fields are not supported: {name}")
                choices = _choice_values(widget)
                if value not in choices:
                    _fail(f"Value is outside allowed choices: {name}")
            plans.append(("ordinary", (widget, value)))
        else:
            _fail(f"Unsupported form field type: {name}")
    # Snapshot first also rejects malformed unrelated radio parent hierarchies,
    # rather than discovering them after changing requested fields.
    expected = _snapshot(doc)
    by_xref = {widget.xref: entry for (_, widget), entry in zip(rows, expected["widgets"])}
    for kind, plan in plans:
        if kind == "radio":
            _, widgets, value = plan
            for xref, export in widgets:
                state = export if value == export else "Off"
                by_xref[xref].update(value=state, state=["name", "/" + state],
                                     group_value=["name", "/" + value])
        else:
            widget, value = plan
            by_xref[widget.xref]["value"] = value
            if widget.field_type == _TYPES["checkbox"]:
                by_xref[widget.xref]["state"] = ["name", "/" + value]
    for kind, plan in plans:
        if kind == "radio":
            parent, widgets, value = plan
            doc.xref_set_key(parent, "V", _pdf_name(value))
            for xref, export in widgets:
                doc.xref_set_key(xref, "AS", _pdf_name(export if value == export else "Off"))
        else:
            widget, value = plan
            widget.field_value = value
            widget.update()
    if _snapshot(doc) != expected:
        _fail("Form engine did not preserve requested values or unrelated field metadata; discard this in-memory document")
    return {"filled_fields": sorted(mapping), "forms_expected": expected}


def _new_spec(doc, spec, existing, occupied):
    allowed = {"page", "name", "type", "rect", "value", "choices", "max_length", "font_size", "label", "required", "read_only"}
    if not isinstance(spec, dict) or set(spec) - allowed:
        _fail("Field specification has unsupported keys or is not an object")
    if type(spec.get("page")) is not int or not 1 <= spec["page"] <= len(doc):
        _fail("Every new field needs a valid one-based page number")
    name = _string(spec.get("name"), "Field name", 128)
    if "." in name:
        _fail("New field names cannot contain hierarchy separators (dots)")
    if name in existing:
        _fail(f"Duplicate or existing field name: {name}")
    kind = spec.get("type")
    if not isinstance(kind, str) or kind not in _TYPES:
        _fail("New field type must be text, checkbox, combobox, or listbox; signature and radio creation are unsupported")
    coords = spec.get("rect")
    if not isinstance(coords, list) or len(coords) != 4 or not all(type(v) in {int, float} and math.isfinite(v) for v in coords):
        _fail("Field rectangles need four finite numeric coordinates")
    rect = pymupdf.Rect(coords)
    page = doc[spec["page"] - 1]
    bounds = page.rect * page.derotation_matrix
    if rect.is_empty or rect.is_infinite or not bounds.contains(rect) or min(rect.width, rect.height) < 12:
        _fail("Field rectangle must be at least 12 points wide/high and fully inside the unrotated page")
    if any((rect & other).get_area() > 0 for other in occupied.get(spec["page"], ())):
        _fail("New field rectangles cannot overlap existing or other new widgets")
    label = _string(spec.get("label", name), "Accessible field label", 256)
    flags = 0
    for key, bit in (("required", 2), ("read_only", 1)):
        value = spec.get(key, False)
        if type(value) is not bool:
            _fail(f"{key} must be true or false")
        if value:
            flags |= bit
    size = spec.get("font_size", 11)
    if type(size) not in {int, float} or not math.isfinite(size) or not 6 <= size <= 36:
        _fail("font_size must be a finite number from 6 to 36 points")
    maxlen = spec.get("max_length", 0)
    if type(maxlen) is not int or not 0 <= maxlen <= _MAX_VALUE or ("max_length" in spec and kind != "text"):
        _fail("max_length is supported only for text and must be an integer from 0 to 100000")
    if "choices" in spec and kind not in {"combobox", "listbox"}:
        _fail("choices is only valid for combobox and listbox fields")
    choices = None
    value = spec.get("value", False if kind == "checkbox" else "")
    if kind == "checkbox":
        if type(value) is not bool:
            _fail("New checkbox value must be true or false")
        if "font_size" in spec:
            _fail("font_size is not configurable for checkbox fields")
    else:
        _string(value, "Initial field value", _MAX_VALUE, empty=True, visible=True)
        if maxlen and len(value) > maxlen:
            _fail("Initial field value exceeds max_length")
        if kind in {"combobox", "listbox"}:
            choices = spec.get("choices")
            if not isinstance(choices, list) or not 1 <= len(choices) <= 200:
                _fail("Choice fields require a nonempty list of at most 200 choices")
            for choice in choices:
                _string(choice, "Choice", 256, visible=True)
            if len(choices) != len(set(choices)):
                _fail("Duplicate choices are ambiguous")
            if value and value not in choices:
                _fail("Initial value is outside allowed choices")
            if kind == "combobox":
                flags |= _COMBO
        if rect.height < size * 1.5 + 4:
            _fail("Field rectangle is too short for font_size")
        # Avoid publishing an initial value or choice label with clipped glyphs.
        for text in ([value] + (choices or [])):
            if pymupdf.get_text_length(text, fontname="helv", fontsize=size) > rect.width - (20 if kind == "combobox" else 8):
                _fail("Initial value or choices do not fit the field width at font_size")
    existing.add(name)
    occupied.setdefault(spec["page"], []).append(rect)
    return {"page": spec["page"], "name": name, "kind": kind, "rect": rect,
            "label": label, "flags": flags, "size": size, "maxlen": maxlen,
            "choices": choices, "value": value}


def create_fields(doc, specs: list) -> dict:
    """Create up to 200 flat, distinctly named, non-overlapping AcroForm fields.

    Coordinates are unrotated page coordinates. Initial text is single-line,
    WinAnsi/Windows-1252, using Helvetica; no scripts or signature creation.
    Labels are AcroForm tooltips, not a claim of PDF/UA accessibility compliance.
    """
    if not isinstance(specs, list) or not 1 <= len(specs) <= _MAX_FIELDS:
        _fail("Field creation needs a nonempty list of at most 200 specifications")
    pages, rows = _widgets(doc)
    if len(rows) + len(specs) > _MAX_WIDGETS:
        _fail("Too many form widgets")
    existing = {widget.field_name for _, widget in rows} | _all_field_names(doc)
    # Also reserve field names that exist only as a nonterminal parent.
    for _, widget in rows:
        for xref in _chain(doc, widget.xref):
            kind, value = doc.xref_get_key(xref, "T")
            if kind == "string":
                existing.add(value)
    occupied = {}
    for page, widget in rows:
        occupied.setdefault(page, []).append(widget.rect)
    plans = [_new_spec(doc, spec, existing, occupied) for spec in specs]
    before = _snapshot(doc)
    for plan in plans:
        widget = pymupdf.Widget()
        widget.field_name = plan["name"]
        widget.field_label = plan["label"]
        widget.field_type = _TYPES[plan["kind"]]
        widget.field_flags = plan["flags"]
        widget.rect = plan["rect"]
        widget.text_font = "Helv"
        widget.text_fontsize = plan["size"]
        widget.text_color = (0, 0, 0)
        widget.border_color = (0.2, 0.2, 0.2)
        widget.border_width = 1
        widget.border_style = "S"
        widget.fill_color = (1, 1, 1)
        if plan["kind"] == "checkbox":
            widget.field_value = "Yes" if plan["value"] else "Off"
            widget.text_font = "ZaDb"
        else:
            widget.field_value = plan["value"]
            if plan["kind"] == "text":
                widget.text_maxlen = plan["maxlen"]
            else:
                widget.choice_values = plan["choices"]
        page = doc[plan["page"] - 1]
        added = page.add_widget(widget)
        # PyMuPDF treats the literal tooltip "Unnamed" as an internal sentinel;
        # writing /TU explicitly preserves exactly the user's validated label.
        doc.xref_set_key(added.xref, "TU", pymupdf.get_pdf_str(plan["label"]))
    expected = _snapshot(doc)
    new_names = {plan["name"] for plan in plans}
    if [entry for entry in expected["widgets"] if entry["name"] not in new_names] != before["widgets"]:
        _fail("Form creation changed unrelated field metadata; discard this in-memory document")
    created = {entry["name"]: entry for entry in expected["widgets"] if entry["name"] in new_names}
    for plan in plans:
        entry = created.get(plan["name"])
        wanted_value = ("Yes" if plan["value"] else "Off") if plan["kind"] == "checkbox" else plan["value"]
        if (not entry or entry["value"] != wanted_value or entry["type"] != _TYPES[plan["kind"]]
                or entry["page"] != plan["page"] or entry["label"] != plan["label"]
                or entry["flags"] != plan["flags"] or entry["max_length"] != plan["maxlen"]
                or entry["choices"] != plan["choices"]
                or any(abs(a - b) > 0.01 for a, b in zip(entry["rect"], plan["rect"]))):
            _fail("Form engine did not preserve the new field specification; discard this in-memory document")
    return {"created_fields": [plan["name"] for plan in plans],
            "forms_expected": expected,
            "form_limits": "New fields use single-line Windows-1252 text, Helvetica, and AcroForm tooltip labels; this does not certify PDF/UA accessibility. Radio and signature creation, scripts, multi-select, and export/display choice pairs are unsupported."}


def verify_forms(path: str | Path, expected: dict, *, flattened: bool = False) -> None:
    """Reopen saved output and verify field metadata, values, and radio states.

    A flattened result must have no widgets. Appearance rendering/flattening is
    covered by real save/reopen raster tests, not a cryptographic guarantee.
    """
    try:
        with pymupdf.open(path) as doc:
            if len(doc) != expected["page_count"]:
                _fail("Saved form verification failed: page count changed")
            actual = _snapshot(doc)
            if flattened:
                if actual["widgets"]:
                    _fail("Saved form verification failed: flattened widgets remain")
                return
            wanted = expected["widgets"]
            if len(actual["widgets"]) != len(wanted):
                _fail("Saved form verification failed: widget count changed")
            for got, want in zip(actual["widgets"], wanted):
                if any(abs(a - b) > 0.01 for a, b in zip(got["rect"], want["rect"])):
                    _fail("Saved form verification failed: field rectangle changed")
                if {k: v for k, v in got.items() if k != "rect"} != {k: v for k, v in want.items() if k != "rect"}:
                    _fail("Saved form verification failed: field metadata, value, or radio state changed")
            pages, rows = _widgets(doc)
            for _, widget in rows:
                if widget.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                    continue
                if widget.field_type in {_RADIO, _TYPES["checkbox"]}:
                    _normal_states(doc, widget)
                elif widget.field_type in {_TYPES["text"], *_CHOICE}:
                    kind, value = doc.xref_get_key(widget.xref, "AP/N")
                    if kind != "xref" or not doc.xref_is_stream(int(value.split()[0])):
                        _fail("Saved form verification failed: missing normal appearance stream")
    except JobError:
        raise
    except Exception as exc:
        raise JobError("Saved form verification failed") from exc
