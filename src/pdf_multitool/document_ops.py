"""Explicit document transformations and audits, kept reusable by future frontends."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
import pymupdf

from .core import JobError, parse_pages


def _pdf_strings_contain(data: bytes, target: str) -> bool:
    """Inspect canonical PDF string values without confusing dictionary keys for text."""
    token = re.compile(rb"\((?:\\.|[^\\)])*\)|<([0-9a-fA-F\s]+)>", re.S)
    for match in token.finditer(data):
        if match[1] is not None:
            try:
                raw = bytes.fromhex(match[1].decode("ascii"))
            except ValueError:
                continue
        else:
            raw = match[0][1:-1]
            raw = re.sub(rb"\\([0-7]{1,3}|.)", lambda m: bytes([int(m[1], 8) % 256]) if re.fullmatch(rb"[0-7]{1,3}", m[1]) else {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}.get(m[1], m[1]), raw)
        for encoding in ("utf-8", "utf-16", "latin-1"):
            try:
                if target.casefold() in raw.decode(encoding).casefold():
                    return True
            except UnicodeError:
                pass
    return False


def fields_report(doc) -> dict:
    fields, signatures = [], []
    for page_no, page in enumerate(doc, 1):
        for widget in page.widgets() or []:
            record = {"page": page_no, "name": widget.field_name,
                      "type": widget.field_type_string,
                      "rect": list(widget.rect), "readonly": bool(widget.field_flags & 1)}
            if widget.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                # PDF field values can be inherited from an indirect parent field.
                current, seen, present = widget.xref, set(), False
                for _ in range(32):
                    if current in seen:
                        present = None
                        break
                    seen.add(current)
                    value_kind, _ = doc.xref_get_key(current, "V")
                    if value_kind != "null":
                        present = True
                        break
                    parent_kind, parent = doc.xref_get_key(current, "Parent")
                    if parent_kind == "null":
                        break
                    if parent_kind != "xref":
                        present = None
                        break
                    try:
                        current = int(parent.split()[0])
                    except (ValueError, IndexError):
                        present = None
                        break
                else:
                    present = None
                record.update(value_present=present, cryptographic_validity="not_verified")
                signatures.append(record)
            else:
                record["value"] = widget.field_value
                if widget.field_type in {pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, pymupdf.PDF_WIDGET_TYPE_CHECKBOX}:
                    from .forms import button_export_value
                    try:
                        record["export_value"] = button_export_value(doc, widget)
                    except JobError as exc:
                        record["fill_support"] = str(exc)
                    record["appearance_state"] = doc.xref_get_key(widget.xref, "AS")[1]
                if widget.field_type in {pymupdf.PDF_WIDGET_TYPE_COMBOBOX, pymupdf.PDF_WIDGET_TYPE_LISTBOX}:
                    record["choices"] = widget.choice_values
                if widget.field_type == pymupdf.PDF_WIDGET_TYPE_TEXT:
                    record["max_length"] = widget.text_maxlen or None
                fields.append(record)
    return {"fields": fields, "signature_fields": signatures,
            "signature_flags": doc.get_sigflags(),
            "signature_warning": "Signature presence is not cryptographic verification. Modifying a signed PDF generally invalidates signatures."}


def transform(doc, operation: str, out: Path, options: dict) -> dict:
    warnings = ["Editing a digitally signed PDF generally invalidates its signatures."]
    detail = {}
    if operation == "clean":
        # Structural cleanup: do not silently remove headers, crop content, or deskew.
        detail = {"transforms": ["rebuild content streams", "remove unreachable objects", "compress streams"],
                  "not_performed": ["deskew", "dewarp", "watermark removal", "automatic header/footer deletion"]}
        doc.save(out, garbage=4, deflate=True, clean=True)
    elif operation == "forms":
        from .forms import create_fields, fill_fields, verify_forms
        mapping, creation = options.get("fields"), options.get("create_fields")
        has_mapping, has_creation = "fields" in options, "create_fields" in options
        flatten = options.get("flatten", False)
        if not isinstance(flatten, bool):
            raise JobError("Form flatten must be true or false")
        if has_mapping and has_creation:
            raise JobError("Choose form filling or field creation, not both in one operation")
        if not has_mapping and not has_creation:
            if flatten:
                raise JobError("Flattening needs an explicit fill or creation operation")
            out.write_text(json.dumps(fields_report(doc), ensure_ascii=False, indent=2), encoding="utf-8")
            return {"detail": {"mode": "inspect"}, "warnings": []}
        detail = create_fields(doc, creation) if has_creation else fill_fields(doc, mapping)
        expected = detail.pop("forms_expected")
        if flatten:
            doc.bake(annots=False, widgets=True)
        doc.save(out, garbage=4, deflate=True)
        verify_forms(out, expected, flattened=flatten)
        detail["flattened"] = flatten
    elif operation == "redact":
        target = options.get("redact_text", "")
        boxes = options.get("redact_boxes", [])
        if not isinstance(target, str) or len(target) > 4096 or not isinstance(boxes, list) or len(boxes) > 1000:
            raise JobError("Invalid redaction specification")
        if not target and not boxes:
            raise JobError("Specify exact text or page rectangles to remove")
        selected = parse_pages(str(options.get("pages", "")), len(doc))
        for box in boxes:
            if not isinstance(box, dict) or type(box.get("page")) is not int or not 1 <= box["page"] <= len(doc):
                raise JobError("Every redaction box needs a valid one-based page number")
            coords = box.get("rect")
            if not isinstance(coords, list) or len(coords) != 4 or not all(type(v) in {int, float} and math.isfinite(v) for v in coords):
                raise JobError("Redaction rectangles require four finite numeric coordinates")
            if box["page"] - 1 not in selected:
                raise JobError("A redaction box lies outside the selected pages")
            rect = pymupdf.Rect(coords)
            page = doc[box["page"] - 1]
            unrotated_bounds = page.rect * page.derotation_matrix
            if rect.is_empty or rect.is_infinite or not unrotated_bounds.contains(rect):
                raise JobError("Redaction rectangle must be inside its page")
        # Never accidentally apply someone else's pending redaction annotations.
        for page in doc:
            for annotation in list(page.annots() or []):
                if annotation.type[0] == pymupdf.PDF_ANNOT_REDACT:
                    page.delete_annot(annotation)
        count = 0
        for index in selected:
            page = doc[index]
            rects = list(page.search_for(target)) if target else []
            for box in boxes:
                if not isinstance(box, dict) or not isinstance(box.get("page"), int):
                    raise JobError("Redaction boxes require one-based page and rect:[x0,y0,x1,y1]")
                if box["page"] == index + 1:
                    rect = pymupdf.Rect(box["rect"])
                    rects.append(rect)
            for rect in rects:
                page.add_redact_annot(rect, fill=(0, 0, 0), cross_out=False)
            if rects:
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                      graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                                      text=pymupdf.PDF_REDACT_TEXT_REMOVE)
                count += len(rects)
        if not count:
            raise JobError("No redaction targets were found; no output was produced")
        # Remove nonvisual reservoirs instead of leaving a false 'secure overlay'.
        doc.scrub(attached_files=True, clean_pages=True, embedded_files=True,
                  hidden_text=True, javascript=True, metadata=True, redactions=False,
                  redact_images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                  remove_links=True, reset_fields=True, reset_responses=True,
                  thumbnails=True, xml_metadata=True)
        doc.set_toc([])
        # Accessibility alternatives can retain removed text after page redaction.
        # These structures cannot be safely rewritten without semantic retagging.
        catalog = doc.pdf_catalog()
        for key in ("StructTreeRoot", "MarkInfo", "Metadata", "PieceInfo", "AF",
                    "OpenAction", "AA", "Collection", "PageLabels", "Names", "Dests", "Perms"):
            doc.xref_set_key(catalog, key, "null")
        for page in doc:
            for key in ("StructParents", "Metadata", "PieceInfo", "AF", "AA", "PresSteps", "Thumb"):
                doc.xref_set_key(page.xref, key, "null")
            for widget in list(page.widgets() or []):
                page.delete_widget(widget)
            for annotation in list(page.annots() or []):
                page.delete_annot(annotation)
        doc.xref_set_key(catalog, "AcroForm", "null")
        doc.save(out, garbage=4, deflate=True, clean=True)
        with pymupdf.open(out) as reopened:
            if target and any(reopened[index].search_for(target) for index in selected):
                out.unlink(missing_ok=True)
                raise JobError("Verification found the targeted text in the output; output discarded")
            if target:
                # Unselected visible content is intentionally preserved; inspect other
                # serialized strings as well as the selected pages' extracted text.
                unselected_streams = {xref for index, page in enumerate(reopened)
                                      if index not in selected for xref in page.get_contents()}
                for xref in range(1, reopened.xref_length()):
                    residual = _pdf_strings_contain(reopened.xref_object(xref).encode("utf-8"), target)
                    if not residual and reopened.xref_is_stream(xref) and xref not in unselected_streams:
                        residual = _pdf_strings_contain(reopened.xref_stream(xref), target)
                    if residual:
                        out.unlink(missing_ok=True)
                        raise JobError("Verification found targeted text in residual PDF data; output discarded")
        detail = {"redacted_regions": count, "removed_nonvisual_content": True}
        warnings.append("Redaction also removes metadata, private application data, attachments, outlines, accessibility tags, links, annotations and form widgets to reduce residual data. Existing pending redaction annotations are discarded without applying them. OCR-visible text inside images requires rectangle redaction; text matching alone cannot find it.")
    elif operation == "unlock":
        doc.save(out, garbage=4, deflate=True, encryption=pymupdf.PDF_ENCRYPT_NONE)
        detail = {"encryption": "removed with supplied document password"}
    elif operation == "protect":
        password = options.get("new_password")
        if not isinstance(password, str) or not 8 <= len(password.encode("utf-8")) <= 127:
            raise JobError("New password must be 8-127 UTF-8 bytes")
        doc.save(out, garbage=4, deflate=True, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                 owner_pw=password, user_pw=password,
                 permissions=pymupdf.PDF_PERM_ACCESSIBILITY | pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_COPY | pymupdf.PDF_PERM_ANNOTATE | pymupdf.PDF_PERM_FORM | pymupdf.PDF_PERM_ASSEMBLE)
        detail = {"encryption": "AES-256"}
    else:
        raise JobError("Unsupported document transformation")
    return {"detail": detail, "warnings": warnings}
