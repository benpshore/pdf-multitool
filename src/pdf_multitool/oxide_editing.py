"""Explicit PDF Oxide editing contracts; originals and native before/after reports survive."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math

from .core import JobError, strict_json_loads

MAX_EDITS = 100
MAX_TEXT = 500_000
MAX_ELEMENTS = 5000
MAX_ATTACHMENT_BYTES = 64 * 1024


def field(label, kind="string", default="", **kwargs):
    return {"label": label, "type": kind, "default": default, **kwargs}


def operation(label, description, options, *, pages=False, inputs=1):
    return {
        "label": label,
        "description": description,
        "engine": "pdf_oxide",
        "inputs": inputs,
        "pages": pages,
        "outputs": ["application/pdf", "application/json"],
        "options": options,
    }


REWRITE = (
    "Produces a new PDF; the original remains unchanged. Rewriting invalidates signatures. "
    "Review text, graphics, links and forms in the output. "
)
OPERATIONS = {
    "oxide_edit_inspect": operation(
        "Inspect PDF Oxide editable structure",
        "Report selected page geometry, text elements, images, outlines, layers, labels and XMP. "
        "Element indices identify this report only; extraction is not a fidelity guarantee.",
        {},
        pages=True,
    ),
    "oxide_edit_pages": operation(
        "Edit PDF Oxide pages",
        REWRITE + "Select, extract, delete or move pages. Page numbers are one-based.",
        {
            "action": field(
                "Page action", default="extract", enum=["extract", "select", "delete", "move"]
            ),
            "destination": field("Move destination page", "integer", 1, minimum=1, maximum=2000),
        },
        pages=True,
    ),
    "oxide_edit_ranges": operation(
        "Split PDF Oxide page ranges",
        REWRITE + "Each JSON pair is an inclusive one-based range; ranges must not overlap.",
        {"ranges": field("Ranges JSON", default="[[1,1]]", multiline=True, maxLength=4096)},
    ),
    "oxide_edit_geometry": operation(
        "Edit PDF Oxide page geometry",
        REWRITE + "Boxes use PDF [left,bottom,right,top] points. "
        "Cropping hides content; it does not erase it.",
        {
            "action": field(
                "Geometry action",
                default="rotate",
                enum=["rotate", "set_rotation", "crop_box", "media_box", "crop_margins"],
            ),
            "degrees": field("Degrees", "integer", 90, enum=[-270, -180, -90, 0, 90, 180, 270]),
            "box": field("Box JSON", default="[0,0,612,792]", maxLength=256),
            "margins": field(
                "Left,right,top,bottom margins JSON", default="[0,0,0,0]", maxLength=256
            ),
        },
        pages=True,
    ),
    "oxide_edit_metadata": operation(
        "Edit PDF Oxide metadata",
        REWRITE + "Set title, author, subject or keywords in the Info dictionary. "
        "Existing XMP can disagree.",
        {
            "metadata": field(
                "Metadata JSON", default='{"title":""}', multiline=True, maxLength=12000
            )
        },
    ),
    "oxide_edit_text": operation(
        "Edit PDF Oxide text elements",
        REWRITE + "Added text uses Helvetica; unsupported glyphs are rejected. "
        "Replacement/removal are unavailable in 0.3.78 because native saving paints black "
        "masks over the affected text. These actions return an explicit error.",
        {
            "action": field("Text action", default="add", enum=["add", "replace", "remove"]),
            "find": field("Literal text to find", maxLength=1024),
            "text": field("Replacement or added text", maxLength=4000, multiline=True),
            "x": field("X points", "number", 72, minimum=-14400, maximum=14400),
            "y": field("Y points", "number", 72, minimum=-14400, maximum=14400),
            "font_size": field("Added text size", "number", 12, minimum=1, maximum=144),
        },
        pages=True,
    ),
    "oxide_edit_images": operation(
        "Edit PDF Oxide image placement",
        REWRITE + "Use page and image names from the structure report. "
        "Changes affect image placement, not encoded pixels.",
        {"changes": field("Image changes JSON", default="[]", multiline=True, maxLength=32000)},
    ),
    "oxide_edit_embed": operation(
        "Embed bytes with PDF Oxide",
        REWRITE + "Embed at most ten named files and 64 KiB total from explicit base64 data. "
        "Attachments are not executed.",
        {"attachments": field("Attachments JSON", default="[]", multiline=True, maxLength=96000)},
    ),
    "oxide_edit_bookmarks": operation(
        "Split PDF Oxide bookmarks",
        REWRITE + "Plan or export bookmark segments with deterministic output names. "
        "Missing outlines are an error.",
        {
            "mode": field("Bookmark action", default="plan", enum=["plan", "split"]),
            "title_prefix": field("Optional title prefix", maxLength=256),
            "ignore_case": field("Ignore prefix case", "boolean", False),
            "level": field(
                "Maximum outline depth (zero: all)", "integer", 1, minimum=0, maximum=32
            ),
            "include_front_matter": field("Include front matter", "boolean", True),
        },
    ),
    "oxide_edit_merge": operation(
        "Merge with PDF Oxide",
        REWRITE + "Append up to 20 supplied PDFs in upload order. "
        "Cross-document forms, outlines and destinations need review.",
        {},
        inputs=2,
    ),
}


def _json(value, label):
    try:
        return strict_json_loads(value)
    except (ValueError, TypeError) as exc:
        raise JobError(f"Invalid {label} JSON") from exc


def _number(value, label, minimum=-14400, maximum=14400):
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise JobError(f"{label} must be a finite number between {minimum} and {maximum}")
    return value


def _integer(value, label, minimum=1, maximum=2000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise JobError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def _string(value, label, maximum=4000, *, empty=True):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value):
        raise JobError(f"{label} must be a bounded string")
    if any(ord(char) < 32 and char not in "\n\t\r" for char in value):
        raise JobError(f"{label} contains unsupported control characters")
    return value


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise JobError("Edit record has missing or unsupported keys")
    return value


def _array(value, maximum=MAX_EDITS):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise JobError(f"Edit array must contain between 1 and {maximum} entries")
    return value


def _quad(value, *, margins=False):
    if not isinstance(value, list) or len(value) != 4:
        raise JobError("Geometry requires exactly four finite numbers")
    for number in value:
        _number(number, "Geometry", minimum=0 if margins else -14400)
    if not margins and (value[0] >= value[2] or value[1] >= value[3]):
        raise JobError("Page box must have positive width and height")
    return value


def _attachment(item):
    _object(item, {"name", "data_base64"}, {"name", "data_base64"})
    name = _string(item["name"], "Attachment name", 128, empty=False)
    if name in {".", ".."} or any(char in name for char in "/\\\r\n\t"):
        raise JobError("Attachment names must be plain file names")
    encoded = _string(item["data_base64"], "Attachment data", 90000)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise JobError("Attachment data must be valid base64") from exc
    return name, data


def validate_options(name, options):
    """Validate structured options before spawn; validate document-dependent bounds in worker."""
    if name == "oxide_edit_metadata":
        data = _object(
            _json(options["metadata"], "metadata"), {"title", "author", "subject", "keywords"}
        )
        if not data:
            raise JobError("Provide at least one metadata field")
        for value in data.values():
            _string(value, "Metadata value", 2000)
    elif name == "oxide_edit_geometry":
        _quad(_json(options["box"], "box"))
        _quad(_json(options["margins"], "margins"), margins=True)
    elif name == "oxide_edit_ranges":
        ranges = _array(_json(options["ranges"], "ranges"))
        used = set()
        total = 0
        for pair in ranges:
            if not isinstance(pair, list) or len(pair) != 2:
                raise JobError("Each range must be [first_page,last_page]")
            start, end = pair
            _integer(start, "Range start")
            _integer(end, "Range end")
            if end < start:
                raise JobError("Range end precedes its start")
            total += end - start + 1
            if total > 100 or used.intersection(range(start, end + 1)):
                raise JobError("Ranges must not overlap and select at most 100 pages")
            used.update(range(start, end + 1))
    elif name == "oxide_edit_text":
        _string(options["text"], "Text")
        if options["action"] in {"replace", "remove"}:
            raise JobError(
                "PDF Oxide 0.3.78 text replacement/removal is unavailable: "
                "native saving paints a black mask over the edited text"
            )
        elif not options["text"]:
            raise JobError("Added text must not be empty")
    elif name == "oxide_edit_images":
        changes = _array(_json(options["changes"], "image changes"))
        for item in changes:
            _object(
                item,
                {"page", "image", "action", "x", "y", "width", "height"},
                {"page", "action"},
            )
            _integer(item["page"], "Image page")
            action = item["action"]
            if action == "clear":
                if set(item) != {"page", "action"}:
                    raise JobError("Clearing pending image edits accepts only page and action")
                continue
            _string(item.get("image"), "Image resource name", 128, empty=False)
            if action not in {"move", "resize", "bounds"}:
                raise JobError("Image action must be move, resize, bounds or clear")
            for key in (
                ("x", "y")
                if action == "move"
                else ("width", "height")
                if action == "resize"
                else ("x", "y", "width", "height")
            ):
                if key not in item:
                    raise JobError("Image change is missing coordinates")
                _number(item[key], key, minimum=0.01 if key in {"width", "height"} else -14400)
    elif name == "oxide_edit_embed":
        items = _array(_json(options["attachments"], "attachments"), 10)
        names = set()
        total = 0
        for item in items:
            filename, data = _attachment(item)
            if filename in names:
                raise JobError("Attachment names must be unique")
            names.add(filename)
            total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise JobError("Attachment bytes exceed 64 KiB")
    return options


def _bounded(value, label, maximum=2 * 1024**2):
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, RecursionError) as exc:
        raise JobError(f"Invalid native {label}") from exc
    if size > maximum:
        raise JobError(f"PDF Oxide {label} limit exceeded")
    return value


def _snapshot(doc, pages, *, elements=False):
    from .oxide_runtime import native

    result = []
    characters = element_count = 0
    for index in pages:
        text = native(
            "editing text snapshot",
            lambda index=index: doc.extract_text(index, extract_tables=False),
        )
        characters += len(text)
        if characters > MAX_TEXT:
            raise JobError("PDF Oxide editing text snapshot limit exceeded")
        row = {
            "page": index + 1,
            "text": text,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "rotation": native("page rotation", lambda index=index: doc.page_rotation(index)),
            "media_box": native("page media box", lambda index=index: doc.page_media_box(index)),
            "crop_box": native("page crop box", lambda index=index: doc.page_crop_box(index)),
            "images": native("editable image metadata", lambda index=index: doc.page_images(index)),
        }
        if elements:
            page = native("page DOM", lambda index=index: doc.page(index))
            row["dom_page"] = {"index": page.index, "width": page.width, "height": page.height}
            texts = native(
                "text element inspection", lambda page=page: page.find_text_containing("")
            )
            children = native("DOM children", page.children)
            element_count += len(children)
            if element_count > MAX_ELEMENTS:
                raise JobError("PDF Oxide text element count limit exceeded")
            row["text_elements"] = [
                {
                    "index": number,
                    "text": text.value,
                    "bbox": text.bbox,
                    "font_name": text.font_name,
                    "font_size": text.font_size,
                    "is_bold": text.is_bold,
                    "is_italic": text.is_italic,
                }
                for number, text in enumerate(texts, 1)
            ]
            row["elements"] = []
            for number, element in enumerate(children, 1):
                record = {
                    "index": number,
                    "bbox": element.bbox,
                    "is_text": element.is_text(),
                    "is_image": element.is_image(),
                    "is_path": element.is_path(),
                    "is_table": element.is_table(),
                    "is_structure": element.is_structure(),
                }
                text = element.as_text()
                image = element.as_image()
                if text is not None:
                    record["text"] = text.text
                if image is not None:
                    record["image"] = {"width": image.width, "height": image.height}
                row["elements"].append(record)
            row["dom_images"] = [
                {
                    "bbox": image.bbox,
                    "width": image.width,
                    "height": image.height,
                    "aspect_ratio": image.aspect_ratio,
                }
                for image in native("DOM image inspection", page.find_images)
            ]
        result.append(_bounded(row, "page report"))
    return result


def _structure(doc):
    from .oxide_runtime import native

    return _bounded(
        {
            "pages": native("page count", lambda: int(doc.page_count)),
            "version": native("version", doc.version),
            "xmp": native("XMP metadata", doc.xmp_metadata),
            "outlines": native("outlines", doc.get_outline),
            "layers": native("layers", doc.get_layers),
            "page_labels": native("page labels", doc.page_labels),
        },
        "document structure",
    )


def _publish_pdf(request, add, name, data, expected_pages, runtime):
    from .oxide_runtime import native, validate_pdf_bytes, write_bytes

    if not isinstance(data, bytes) or not data.startswith(b"%PDF-"):
        raise JobError("PDF Oxide editing did not produce PDF bytes")
    if len(data) > min(request["limits"]["max_output_bytes"], 16 * 1024**2):
        raise JobError("PDF Oxide edited PDF exceeds its output byte limit")
    validate_pdf_bytes(data, expected_pages=expected_pages)
    document = native("reopen edited PDF", lambda: runtime.PdfDocument.from_bytes(data))
    count = native("edited page count", lambda: int(document.page_count))
    if count != expected_pages:
        raise JobError("PDF Oxide edited page count differs from the requested operation")
    output = {
        "file": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "pages": count,
    }
    # These are source-engine checks, not independent visual or semantic validation.
    output["structure"] = _structure(document)
    output["page_snapshots"] = _snapshot(document, range(min(count, 100)))
    output["snapshot_scope"] = "first 100 pages at most; all output pages counted"
    write_bytes(request, add, name, data)
    return output


def _page_edit(doc, options, pages, count):
    from .oxide_runtime import native

    action = options["action"]
    if action == "extract":
        return native("extract pages", lambda: doc.extract_pages_to_bytes(pages)), len(pages)
    if action == "select":
        native("select pages", lambda: doc.select_pages(pages))
        count = len(pages)
    elif action == "delete":
        if len(pages) == count:
            raise JobError("Deleting every page is unsupported; retain at least one page")
        for page in sorted(pages, reverse=True):
            native("delete page", lambda page=page: doc.delete_page(page))
        count -= len(pages)
    elif action == "move":
        if len(pages) != 1 or options["destination"] > count:
            raise JobError(
                "Move requires exactly one selected page and a destination within the document"
            )
        native("move page", lambda: doc.move_page(pages[0], options["destination"] - 1))
    return native("serialize edited pages", doc.to_bytes), count


def _geometry(doc, options, pages, count):
    from .oxide_runtime import native

    action = options["action"]
    if action == "rotate" and pages == list(range(count)):
        native("rotate all pages", lambda: doc.rotate_all_pages(options["degrees"]))
        return
    if action == "crop_margins":
        if pages != list(range(count)):
            raise JobError("Native crop_margins applies to all pages; select the whole document")
        margins = _quad(_json(options["margins"], "margins"), margins=True)
        for index in pages:
            box = native("crop bounds", lambda index=index: doc.page_media_box(index))
            if (
                margins[0] + margins[1] >= box[2] - box[0]
                or margins[2] + margins[3] >= box[3] - box[1]
            ):
                raise JobError("Crop margins leave an empty page")
        native("crop margins", lambda: doc.crop_margins(*margins))
        return
    box = _quad(_json(options["box"], "box"))
    for index in pages:
        if action == "rotate":
            native("rotate page", lambda index=index: doc.rotate_page(index, options["degrees"]))
        elif action == "set_rotation":
            native(
                "set rotation",
                lambda index=index: doc.set_page_rotation(index, options["degrees"] % 360),
            )
        elif action == "crop_box":
            native("set crop box", lambda index=index: doc.set_page_crop_box(index, *box))
        else:
            native("set media box", lambda index=index: doc.set_page_media_box(index, *box))


def _text_edit(doc, options, pages):
    from .oxide_runtime import native

    changes = []
    for index in pages:
        page = native("editable page", lambda index=index: doc.page(index))
        native(
            "add text element",
            lambda page=page: page.add_text(
                options["text"], options["x"], options["y"], options["font_size"]
            ),
        )
        changes.append({"page": index + 1, "action": "add", "after": options["text"]})
        native("save edited page", lambda page=page: doc.save_page(page))
        _bounded(changes, "text changes")
    return changes


def _image_edit(doc, changes, count):
    from .oxide_runtime import native

    report = []
    for item in changes:
        index = item["page"] - 1
        if index >= count:
            raise JobError("Image edit page is outside the document")
        if item["action"] == "clear":
            native(
                "clear pending image edits",
                lambda index=index: doc.clear_image_modifications(index),
            )
            pending = native(
                "pending image edits", lambda index=index: doc.has_image_modifications(index)
            )
            if pending:
                raise JobError("PDF Oxide failed to clear pending image edits")
            report.append({"request": item, "pending_modifications": pending})
            continue
        images = native("image names", lambda index=index: doc.page_images(index))
        matching = [image for image in images if image["name"] == item["image"]]
        if len(matching) != 1:
            raise JobError("Image resource must identify exactly one image placement")
        if item["action"] == "move":
            native(
                "move image",
                lambda index=index, item=item: doc.reposition_image(
                    index, item["image"], item["x"], item["y"]
                ),
            )
        elif item["action"] == "resize":
            native(
                "resize image",
                lambda index=index, item=item: doc.resize_image(
                    index, item["image"], item["width"], item["height"]
                ),
            )
        else:
            native(
                "set image bounds",
                lambda index=index, item=item: doc.set_image_bounds(
                    index, item["image"], item["x"], item["y"], item["width"], item["height"]
                ),
            )
        pending = native(
            "pending image edits", lambda index=index: doc.has_image_modifications(index)
        )
        report.append({"request": item, "before": matching[0], "pending_modifications": pending})
    return report


def _verify_changed_text(data, changes):
    """Reject observed missing glyphs using a second parser; layout needs visual review."""
    import pymupdf

    def compact(text):
        return "".join(text.split())

    with pymupdf.open(stream=data, filetype="pdf") as document:
        pages = {}
        for change in changes:
            index = change["page"] - 1
            if index not in pages:
                text = document[index].get_text()
                if len(text) > MAX_TEXT:
                    raise JobError("Edited text verification exceeds its character limit")
                pages[index] = compact(text)
            expected = compact(change["after"] or "")
            if expected and expected not in pages[index]:
                raise JobError(
                    "PDF Oxide did not retain the requested replacement glyphs/text; "
                    "no derivative published"
                )


def process(request, options, add):
    from .oxide_runtime import native, open_source, require_runtime, selected, write_json

    runtime = require_runtime(request)
    operation_name = request["operation"]
    validate_options(operation_name, options)
    doc, original, source = open_source(request, options)
    count = source["pages"]
    pages = (
        selected(options, count)
        if OPERATIONS[operation_name]["pages"]
        else list(range(min(count, 100)))
    )
    before = _snapshot(doc, pages, elements=operation_name == "oxide_edit_inspect")
    manifest = {
        "engine": "pdf-oxide",
        "version": "0.3.78",
        "operation": operation_name,
        "source": source,
        "source_bytes_preserved": True,
        "before": {
            "structure": _structure(doc),
            "page_snapshots": before,
            "snapshot_pages": [p + 1 for p in pages],
            "snapshot_scope": "listed pages only",
        },
        "outputs": [],
        "semantics_verified": False,
        "visual_review": "pending",
        "truncated": False,
        "warnings": [],
    }
    warnings = []
    data = None
    expected = count
    if operation_name == "oxide_edit_inspect":
        pass
    elif operation_name == "oxide_edit_pages":
        data, expected = _page_edit(doc, options, pages, count)
    elif operation_name == "oxide_edit_ranges":
        ranges = _json(options["ranges"], "ranges")
        if any(end > count for _, end in ranges):
            raise JobError("Page range is outside the document")
        native_ranges = [(start - 1, end) for start, end in ranges]
        parts = native(
            "extract page ranges", lambda: doc.extract_page_ranges_to_bytes(native_ranges)
        )
        if len(parts) != len(ranges):
            raise JobError("PDF Oxide range output count differs from the plan")
        for number, (part, pair) in enumerate(zip(parts, ranges, strict=True), 1):
            manifest["outputs"].append(
                _publish_pdf(
                    request, add, f"range-{number:04d}.pdf", part, pair[1] - pair[0] + 1, runtime
                )
            )
        manifest["ranges"] = ranges
    elif operation_name == "oxide_edit_geometry":
        _geometry(doc, options, pages, count)
        manifest["geometry_changes"] = {
            "action": options["action"],
            "pages": [index + 1 for index in pages],
            "degrees": options["degrees"],
            "absolute_degrees": options["degrees"] % 360,
            "box": _json(options["box"], "box"),
            "margins": _json(options["margins"], "margins"),
        }
        data = native("serialize geometry edits", doc.to_bytes)
    elif operation_name == "oxide_edit_metadata":
        metadata = _json(options["metadata"], "metadata")
        setters = {
            "title": doc.set_title,
            "author": doc.set_author,
            "subject": doc.set_subject,
            "keywords": doc.set_keywords,
        }
        for key, value in metadata.items():
            native("set metadata", lambda key=key, value=value: setters[key](value))
        manifest["metadata_changes"] = metadata
        warnings.append("Info fields were changed; existing XMP is retained and may disagree.")
        data = native("serialize metadata", doc.to_bytes)
    elif operation_name == "oxide_edit_text":
        manifest["text_changes"] = _text_edit(doc, options, pages)
        warnings.append(
            "Text-element edits can change layout or omit unavailable glyphs; "
            "compare every changed page."
        )
        data = native("serialize text edits", doc.to_bytes)
        _verify_changed_text(data, manifest["text_changes"])
        manifest["changed_text_check"] = (
            "Requested nonempty strings were found by an independent parser after "
            "whitespace normalization. This is not a visual/layout guarantee."
        )
    elif operation_name == "oxide_edit_images":
        manifest["image_changes"] = _image_edit(
            doc, _json(options["changes"], "image changes"), count
        )
        data = native("serialize image placement", doc.to_bytes)
    elif operation_name == "oxide_edit_embed":
        attachments = []
        for item in _json(options["attachments"], "attachments"):
            filename, blob = _attachment(item)
            native(
                "embed file bytes",
                lambda filename=filename, blob=blob: doc.embed_file(filename, blob),
            )
            attachments.append(
                {"name": filename, "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
            )
        manifest["embedded_files"] = attachments
        data = native("serialize attachments", doc.to_bytes)
    elif operation_name == "oxide_edit_bookmarks":
        kwargs = {key: options[key] for key in ("ignore_case", "level", "include_front_matter")}
        kwargs["title_prefix"] = options["title_prefix"] or None
        plan = native("bookmark plan", lambda: runtime.plan_split_by_bookmarks(original, **kwargs))
        if not 1 <= len(plan) <= 100 or any(
            not 0 <= item["start_page"] < item["end_page"] <= count for item in plan
        ):
            raise JobError("PDF Oxide bookmark plan exceeds its page or segment bounds")
        manifest["bookmark_plan"] = _bounded(plan, "bookmark plan")
        if options["mode"] == "split":
            parts = native("bookmark split", lambda: runtime.split_by_bookmarks(original, **kwargs))
            if [item for item, _ in parts] != plan:
                raise JobError("PDF Oxide bookmark output differs from its plan")
            for number, (segment, part) in enumerate(parts, 1):
                manifest["outputs"].append(
                    _publish_pdf(
                        request,
                        add,
                        f"bookmark-{number:04d}.pdf",
                        part,
                        segment["end_page"] - segment["start_page"],
                        runtime,
                    )
                )
    elif operation_name == "oxide_edit_merge":
        if not 2 <= len(request["inputs"]) <= 20:
            raise JobError("PDF Oxide merge requires between 2 and 20 PDFs")
        sources = [source]
        for index in range(1, len(request["inputs"])):
            single = {
                **request,
                "inputs": [request["inputs"][index]],
                "names": [request["names"][index]],
            }
            _, blob, report = open_source(single, options)
            expected += report["pages"]
            if expected > min(request["limits"]["max_pages"], 2000):
                raise JobError("Merged PDF exceeds its page limit")
            added = native("merge PDF bytes", lambda blob=blob: doc.merge_from(blob))
            if added != report["pages"]:
                raise JobError("PDF Oxide merge page count differs from source")
            sources.append(report)
        manifest["sources"] = sources
        data = native("serialize merged PDF", doc.to_bytes)
    else:
        raise JobError("Unknown PDF Oxide editing operation")
    if data is not None:
        manifest["outputs"].append(
            _publish_pdf(request, add, "edited.pdf", data, expected, runtime)
        )
    if manifest["outputs"]:
        warnings.append(
            "A new PDF was written; original signatures are invalid in the derivative. "
            "Visual and semantic accuracy remain unverified."
        )
    manifest["warnings"] = warnings
    manifest["structured_warnings"] = _bounded(
        native("editing diagnostics", doc.structured_warnings), "diagnostics", 256 * 1024
    )
    write_json(request, add, "oxide-editing-manifest.json", manifest)
    return warnings, False
