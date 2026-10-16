"""Bounded PDF Oxide geometry, search and explicitly provisioned offline OCR."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
import stat
from pathlib import Path

from .core import JobError, strict_json_loads

VERSION = "0.3.78"
ONNXRUNTIME_VERSION = "1.31.0"
MAX_NODES = 250_000
MAX_TEXT = 500_000
MAX_NATIVE_JSON = 8 * 1024**2
MAX_MODELS_BYTES = 512 * 1024**2
PROFILES = (
    "conservative",
    "aggressive",
    "balanced",
    "academic",
    "policy",
    "form",
    "government",
    "scanned_ocr",
    "adaptive",
)
MODES = (
    "all",
    "text",
    "chars",
    "spans",
    "words",
    "text_lines",
    "page_text",
    "paths",
    "rects",
    "lines",
    "layout",
    "layers_inks",
    "classification",
    "structured",
    "rich_text",
    "tables",
    "images",
)


def _field(label, kind="string", default="", **kwargs):
    return {"label": label, "type": kind, "default": default, **kwargs}


OPERATIONS = {
    "oxide_extract_mask": {
        "label": "Mask an additional extraction copy",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": True,
        "description": (
            "Apply explicit page/region extraction masks to a separate document instance. "
            "Retain original and derived text/spans. This does not redact or rewrite the PDF."
        ),
        "outputs": ["application/json", "text/plain"],
        "options": {"actions_json": _field("Mask actions (JSON)", default="[]", maxLength=32768)},
    },
    "oxide_analyze": {
        "label": "Analyze with PDF Oxide",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": True,
        "description": (
            "Inspect native text geometry, vectors, layout, layers, inks and classification. "
            "Results retain source/page identity; inferred reading order and classification "
            "require review. No OCR or model downloads."
        ),
        "outputs": ["application/json"],
        "options": {
            "mode": _field("Analysis", default="all", enum=list(MODES)),
            "span_order": _field(
                "Span order",
                default="top_to_bottom",
                enum=[
                    "top_to_bottom",
                    "column_aware",
                    "structure",
                ],
            ),
            "profile": _field("Word/line profile", default="native", enum=["native", *PROFILES]),
            "include_artifacts": _field("Include tagged artifacts", "boolean", True),
            "filters_json": _field(
                "Optional region or layer filters (JSON)", default="{}", maxLength=8192
            ),
            "document_classification": _field("Also classify whole document", "boolean", False),
        },
    },
    "oxide_search": {
        "label": "Search with PDF Oxide",
        "engine": "pdf_oxide",
        "inputs": 1,
        "pages": True,
        "description": "Search native text with page identity and coordinates; OCR is not run.",
        "outputs": ["application/json"],
        "options": {
            "pattern": _field("Search pattern", maxLength=512),
            "literal": _field("Literal text", "boolean", True),
            "case_insensitive": _field("Ignore case", "boolean", False),
            "whole_word": _field("Whole word", "boolean", False),
            "scope": _field("Scope", default="selected_pages", enum=["selected_pages", "document"]),
            "prepare_index": _field("Prepare document index", "boolean", False),
            "max_results": _field("Maximum matches", "integer", 1000, minimum=1, maximum=1000),
        },
    },
    "oxide_ocr": {
        "label": "OCR with PDF Oxide",
        "engine": "pdf_oxide_ocr",
        "inputs": 1,
        "pages": True,
        "description": (
            "Use an explicitly provisioned, hash-checked local detector/recognizer/dictionary. "
            "Native text is retained separately. No downloads or fallback to unverified models."
        ),
        "outputs": ["application/json", "text/plain"],
        "options": {
            "det_threshold": _field("Detection threshold", "number", 0.3, minimum=0.01, maximum=1),
            "rec_threshold": _field(
                "Recognition threshold", "number", 0.5, minimum=0.01, maximum=1
            ),
            "num_threads": _field("CPU threads", "integer", 2, minimum=1, maximum=2),
        },
    },
}


def _filters(options):
    try:
        filters = strict_json_loads(options["filters_json"])
    except (ValueError, TypeError) as exc:
        raise JobError("Analysis filters must be valid JSON") from exc
    allowed = {
        "region",
        "exclude_layers",
        "exclude_inks",
        "word_gap_threshold",
        "line_gap_threshold",
    }
    if not isinstance(filters, dict) or filters.keys() - allowed:
        raise JobError("Unsupported analysis filter")
    mode = options["mode"]
    applicable = {
        "region": {
            "text",
            "chars",
            "spans",
            "words",
            "text_lines",
            "paths",
            "rects",
            "lines",
            "tables",
            "images",
        },
        "exclude_layers": {"text", "chars"},
        "exclude_inks": {"text", "chars"},
        "word_gap_threshold": {"words", "text_lines"},
        "line_gap_threshold": {"text_lines"},
    }
    for key, value in filters.items():
        if mode not in applicable[key]:
            raise JobError(f"{key} is not supported for the selected analysis mode")
        if key == "region":
            if (
                not isinstance(value, list)
                or len(value) != 4
                or any(
                    type(v) not in {int, float} or not math.isfinite(v) or abs(v) > 100_000
                    for v in value
                )
                or value[2] <= 0
                or value[3] <= 0
            ):
                raise JobError("Region must be finite [x,y,width,height] with positive dimensions")
            if mode == "spans" and options["span_order"] != "top_to_bottom":
                raise JobError("Pinned PDF Oxide ignores custom span order with a region")
        elif key.startswith("exclude_"):
            if (
                not isinstance(value, list)
                or len(value) > 64
                or any(not isinstance(v, str) or not v or len(v) > 256 for v in value)
            ):
                raise JobError("Layer/ink exclusions require at most 64 bounded names")
        elif type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= 100:
            raise JobError("Layout thresholds must be finite and between 0 and 100")
    if options["profile"] != "native" and mode not in {"words", "text_lines"}:
        raise JobError("Extraction profiles apply only to word or text-line analysis")
    if not options["include_artifacts"] and mode not in {"text", "words", "text_lines"}:
        raise JobError("Artifact exclusion applies only to text, word or text-line analysis")
    if mode == "text" and filters and not options["include_artifacts"]:
        raise JobError("Pinned filtered text extraction ignores artifact exclusion")
    if mode == "text" and filters:
        raise JobError(
            "Pinned PDF Oxide filtered text can reinsert out-of-region table content; "
            "use bounded region spans or words and compare with the source"
        )
    return filters


def validate_options(operation, options):
    """Additional validation after the catalog's scalar/schema validation."""
    if operation == "oxide_extract_mask":
        _mask_actions(options)
    elif operation == "oxide_analyze":
        _filters(options)
    elif operation == "oxide_search":
        if not options["pattern"]:
            raise JobError("A nonempty search pattern is required")
        if options["scope"] == "document" and options["pages"].strip():
            raise JobError("Document-wide search cannot also specify a page selection")


def _mask_actions(options):
    try:
        actions = strict_json_loads(options["actions_json"])
    except (ValueError, TypeError) as exc:
        raise JobError("Mask actions must be valid JSON") from exc
    if not isinstance(actions, list) or not 1 <= len(actions) <= 100:
        raise JobError("Extraction masking requires 1..100 explicit actions")
    total_rectangles = 0
    for action in actions:
        if not isinstance(action, dict) or set(action) - {"action", "page", "rectangles"}:
            raise JobError("Unsupported extraction mask action fields")
        kind = action.get("action")
        if kind not in {"region", "regions", "header", "footer", "artifacts", "clear"}:
            raise JobError("Unsupported extraction mask action")
        if type(action.get("page")) is not int or not 1 <= action["page"] <= 2000:
            raise JobError("Extraction mask page must be in 1..2000")
        rectangles = action.get("rectangles", [])
        if not isinstance(rectangles, list):
            raise JobError("Extraction rectangles must be a list")
        if kind in {"region", "regions"}:
            if not rectangles or (kind == "region" and len(rectangles) != 1):
                raise JobError(
                    "Region action requires one rectangle; regions requires a nonempty list"
                )
        elif rectangles:
            raise JobError("Only region actions accept rectangles")
        total_rectangles += len(rectangles)
        if total_rectangles > 100:
            raise JobError("Extraction masking permits at most 100 rectangles")
        for box in rectangles:
            if (
                not isinstance(box, list)
                or len(box) != 4
                or any(
                    type(v) not in {int, float} or not math.isfinite(v) or abs(v) > 14400
                    for v in box
                )
                or box[2] <= box[0]
                or box[3] <= box[1]
            ):
                raise JobError("Mask rectangle must be finite positive [llx,lly,urx,ury]")
    return actions


def _mask(request, options, add):
    from .oxide_runtime import (
        native,
        open_source,
        require_runtime,
        selected,
        write_bytes,
        write_json,
    )

    module = require_runtime(request)
    original, data, source = open_source(request, options)
    pages = selected(options, source["pages"])
    actions = _mask_actions(options)
    if any(action["page"] - 1 not in pages for action in actions):
        raise JobError("Every mask action must target an existing selected page")
    derived = native(
        "mask document copy",
        lambda: module.PdfDocument.from_bytes(data, options.get("password", "")),
    )
    for action in actions:
        index = action["page"] - 1
        kind = action["action"]
        rectangles = [tuple(box) for box in action.get("rectangles", [])]
        if kind == "region":
            native(
                "extraction region mask",
                lambda i=index, r=rectangles[0]: derived.erase_region(i, *r),
            )
        elif kind == "regions":
            native(
                "extraction region masks", lambda i=index, r=rectangles: derived.erase_regions(i, r)
            )
        else:
            methods = {
                "header": derived.erase_header,
                "footer": derived.erase_footer,
                "artifacts": derived.erase_artifacts,
                "clear": derived.clear_erase_regions,
            }
            native("extraction mask action", lambda method=methods[kind], i=index: method(i))
    budget = _ContentBudget()
    manifest = {
        "schema_version": 1,
        "engine": {"name": "pdf-oxide", "version": VERSION},
        "source": source,
        "actions": actions,
        "action_coordinates": (
            "Native PDF points [llx,lly,urx,ury]; crop/rotation mapping unqualified"
        ),
        "source_modified": False,
        "pdf_derivative_written": False,
        "secure_redaction": False,
        "semantics_verified": False,
        "pages": [],
    }
    partial = False
    for index in pages:
        baseline = budget.convert(
            native(
                "original extraction",
                lambda i=index: {
                    "text": original.extract_text(i, extract_tables=False, include_artifacts=True),
                    "spans": original.extract_spans(i),
                },
            )
        )
        masked = budget.convert(
            native(
                "masked extraction",
                lambda i=index: {
                    "text": derived.extract_text(i, extract_tables=False, include_artifacts=True),
                    "spans": derived.extract_spans(i),
                },
            )
        )
        page = {
            "page": index + 1,
            "native_page_index": index,
            "source_sha256": source["sha256"],
            "original": baseline,
            "derived": masked,
            "text_changed": baseline["text"] != masked["text"],
            "text_status": "extracted" if masked["text"] else "empty",
            **_page_diagnostics(derived, index),
        }
        partial = partial or page["truncated"]
        filename = f"masked-page-{index + 1:04d}.json"
        write_json(request, add, filename, page)
        write_bytes(
            request, add, f"masked-page-{index + 1:04d}-original.txt", baseline["text"].encode()
        )
        write_bytes(
            request, add, f"masked-page-{index + 1:04d}-derived.txt", masked["text"].encode()
        )
        manifest["pages"].append(
            {"page": index + 1, "file": filename, "changed": page["text_changed"]}
        )
    manifest["original_structured_warnings"] = _diagnostics(original, source, budget)
    manifest["derived_structured_warnings"] = _diagnostics(derived, source, budget)
    write_json(request, add, "oxide-mask-manifest.json", manifest)
    return [
        "Extraction masks may omit meaningful content. Compare both copies. "
        "The source PDF is unchanged and has not been redacted."
    ], partial


ATTRIBUTES = {
    "TextChar": (
        "char",
        "bbox",
        "font_name",
        "font_size",
        "font_weight",
        "is_italic",
        "is_monospace",
        "color",
        "rotation_degrees",
        "origin_x",
        "origin_y",
        "advance_width",
        "mcid",
    ),
    "TextSpan": (
        "text",
        "bbox",
        "page_bbox",
        "font_name",
        "font_size",
        "is_bold",
        "is_italic",
        "is_monospace",
        "char_widths",
        "color",
        "sequence",
        "provenance",
    ),
    "TextWord": (
        "text",
        "bbox",
        "font_name",
        "font_size",
        "is_bold",
        "is_italic",
        "chars",
        "sequence",
        "rotation_degrees",
    ),
    "TextLine": ("text", "bbox", "words", "chars"),
    "LayoutParams": (
        "word_gap_threshold",
        "line_gap_threshold",
        "median_char_width",
        "median_font_size",
        "median_line_spacing",
        "column_count",
    ),
    "ExtractionProfile": (
        "name",
        "tj_offset_threshold",
        "word_margin_ratio",
        "space_threshold_em_ratio",
        "space_char_multiplier",
        "use_adaptive_threshold",
    ),
}


class _ContentBudget:
    def __init__(self):
        self.nodes = self.characters = 0

    def convert(self, value, depth=0):
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > 32:
            raise JobError("PDF Oxide analysis item/depth limit exceeded; no output kept")
        if isinstance(value, str):
            self.characters += len(value)
            if self.characters > MAX_TEXT:
                raise JobError("PDF Oxide analysis text limit exceeded; no output kept")
            return value
        if value is None or type(value) in {bool, int}:
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise JobError("PDF Oxide returned a non-finite coordinate")
            return value
        if isinstance(value, dict):
            if any(not isinstance(k, str) for k in value):
                raise JobError("PDF Oxide returned an unsupported object key")
            return {k: self.convert(v, depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.convert(v, depth + 1) for v in value]
        attributes = ATTRIBUTES.get(type(value).__name__)
        if attributes is None:
            raise JobError("PDF Oxide returned an unsupported analysis object")
        return {k: self.convert(getattr(value, k), depth + 1) for k in attributes}


def _native_json(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_NATIVE_JSON:
        raise JobError("PDF Oxide structured result exceeds its limit")
    try:
        return strict_json_loads(text)
    except (ValueError, TypeError) as exc:
        raise JobError("PDF Oxide returned invalid structured JSON") from exc


def _diagnostics(doc, source, budget):
    from .oxide_runtime import native

    records = native("analysis diagnostics", doc.structured_warnings)
    if len(records) > 2000:
        raise JobError("PDF Oxide diagnostic count exceeded")
    return budget.convert(
        [
            {
                "source_sha256": source["sha256"],
                "page": record["page"] + 1 if record.get("page") is not None else None,
                "region": None,
                "native": record,
            }
            for record in records
        ]
    )


def _page_diagnostics(doc, index):
    from .oxide_runtime import native

    records = native("page diagnostic status", doc.structured_warnings)
    if len(records) > 2000:
        raise JobError("PDF Oxide diagnostic count exceeded")
    categories = {value["category"] for value in records if value.get("page") in {None, index}}
    return {
        "truncated": "operator_cap_exceeded" in categories,
        "ocr_required": True if "no_text_layer" in categories else None,
        "diagnostic_status": "warnings_present" if categories else "none_reported",
    }


def _page_calls(doc, index, options, module):
    filters = _filters(options)
    region = tuple(filters["region"]) if "region" in filters else None
    if region is not None:
        filters["region"] = region
    profile = (
        None
        if options["profile"] == "native"
        else getattr(module.ExtractionProfile, options["profile"])()
    )
    word_options = {
        "include_artifacts": options["include_artifacts"],
        **({"region": region} if region else {}),
        **({"profile": profile} if profile else {}),
        **(
            {"word_gap_threshold": filters["word_gap_threshold"]}
            if "word_gap_threshold" in filters
            else {}
        ),
    }
    return {
        "text": lambda: doc.extract_text(
            index, **filters, extract_tables=False, include_artifacts=options["include_artifacts"]
        ),
        "chars": lambda: doc.extract_chars(index, **filters),
        "spans": lambda: doc.extract_spans(
            index, region=region, reading_order=options["span_order"]
        ),
        "words": lambda: doc.extract_words(index, **word_options),
        "text_lines": lambda: doc.extract_text_lines(
            index,
            **word_options,
            **(
                {"line_gap_threshold": filters["line_gap_threshold"]}
                if "line_gap_threshold" in filters
                else {}
            ),
        ),
        "page_text": lambda: doc.extract_page_text(index, reading_order=options["span_order"]),
        "paths": lambda: doc.extract_paths(index, region=region),
        "rects": lambda: doc.extract_rects(index, region=region),
        "lines": lambda: doc.extract_lines(index, region=region),
        "layout": lambda: doc.page_layout_params(index),
        "layers_inks": lambda: {
            "document_layers": doc.get_layers(),
            "page_inks": doc.get_page_inks(index),
            "page_inks_deep": doc.get_page_inks_deep(index),
        },
        "classification": lambda: {
            "page": _native_json(doc.classify_page(index)),
            "has_text_layer": doc.has_text_layer(index),
        },
        "structured": lambda: _native_json(doc.extract_structured(index)),
        "rich_text": lambda: _native_json(
            doc.extract_page_auto(
                index,
                options_json=json.dumps(
                    {
                        "mode": "text_only",
                        "reconstruct_image_tables": False,
                        "emit_placeholders": True,
                        "force_ocr_pages": [],
                        "ocr_languages": [],
                    }
                ),
            )
        ),
        "tables": lambda: doc.extract_tables(index, region=region),
        "images": lambda: doc.extract_images(index, region=region),
    }


def _analyze(request, options, add):
    from .oxide_runtime import native, open_source, require_runtime, selected, write_json

    module = require_runtime(request)
    doc, _, source = open_source(request, options)
    count = source["pages"]
    pages = selected(options, count)
    budget = _ContentBudget()
    from pdf_oxide import pdf_oxide as extension

    report = {
        "schema_version": 1,
        "engine": {"name": "pdf-oxide", "version": VERSION},
        "source": source,
        "settings": {k: v for k, v in options.items() if k != "password"},
        "coordinate_convention": "Native [x,y,width,height]; crop/rotation mapping unqualified",
        "semantics_verified": False,
        "ocr": "not_run",
        "pages": [],
        "profile_catalog": budget.convert(
            [getattr(module.ExtractionProfile, name)() for name in PROFILES]
        ),
        "native_profile_names": native("profile names", module.ExtractionProfile.available),
        "runtime_capabilities": {
            "compiled_ocr_feature": native("OCR feature inspection", extension.prefetch_available),
            "model_downloads_allowed": False,
            "upstream_model_registry": budget.convert(
                native(
                    "model registry inspection", lambda: _native_json(extension.model_manifest())
                )
            ),
            "model_registry_qualification": (
                "Upstream URLs include mutable revisions and have no hashes; this registry "
                "is informational and is never used for automatic provisioning"
            ),
            "log_level": native("logging inspection", module.get_log_level),
        },
        "upstream_limitations": [
            "Classification is heuristic, not proof that OCR is required or text is complete.",
            "Structured regions are not a substitute for direct table rows/cells.",
            "Layout parameters may use an upstream fallback page box.",
            "has_structure_tree returns false after some native parsing errors.",
            "Filtered text extraction can reinsert out-of-region tables and is blocked.",
        ],
    }
    partial = False
    if options["document_classification"]:
        if count > 100:
            raise JobError("Whole-document classification is limited to 100 pages")
        report["document_classification"] = budget.convert(
            native("document classification", lambda: _native_json(doc.classify_document()))
        )
    report["has_structure_tree"] = native("structure presence", doc.has_structure_tree)
    for index in pages:
        page = {
            "page": index + 1,
            "native_page_index": index,
            "source_sha256": source["sha256"],
            "analysis": {},
            "truncated": False,
        }
        calls = _page_calls(doc, index, options, module)
        names = list(calls) if options["mode"] == "all" else [options["mode"]]
        for name in names:
            # Native failures are explicit per-analysis outcomes. Budget failures below
            # abort the file instead of publishing a misleading truncated success.
            try:
                data = native(f"page {index + 1} {name}", calls[name])
            except JobError as exc:
                page["analysis"][name] = {"status": "error", "error": str(exc)}
                partial = True
            else:
                converted = budget.convert(data)
                page["analysis"][name] = {
                    "status": "empty" if converted in ([], {}, "") else "extracted",
                    "native": converted,
                }
        page.update(_page_diagnostics(doc, index))
        partial = partial or page["truncated"]
        filename = f"analysis-page-{index + 1:04d}.json"
        write_json(request, add, filename, page)
        report["pages"].append(
            {
                "page": index + 1,
                "file": filename,
                "statuses": {name: value["status"] for name, value in page["analysis"].items()},
            }
        )
    report["structured_warnings"] = _diagnostics(doc, source, budget)
    report["partial"] = partial
    write_json(request, add, "oxide-analysis-manifest.json", report)
    warnings = ["Native geometry, inferred reading order and classification require source review."]
    if partial:
        warnings.append("Some requested native analyses failed; see explicit per-page errors.")
    if report["structured_warnings"]:
        warnings.append("PDF Oxide reported structured diagnostics; inspect the manifest.")
    return warnings, partial


def _search(request, options, add):
    from .oxide_runtime import native, open_source, selected, write_json

    doc, _, source = open_source(request, options)
    pages = selected(options, source["pages"])
    if (options["scope"] == "document" or options["prepare_index"]) and source["pages"] > 100:
        raise JobError("Whole-document search/index preparation is limited to 100 pages")
    kwargs = {k: options[k] for k in ("case_insensitive", "literal", "whole_word")}
    matches = []
    maximum = options["max_results"]
    try:
        if options["prepare_index"]:
            native("search index preparation", doc.prepare_search)
        if options["scope"] == "document":
            matches = native(
                "document search",
                lambda: doc.search(options["pattern"], max_results=maximum + 1, **kwargs),
            )
        else:
            for index in pages:
                matches.extend(
                    native(
                        f"page {index + 1} search",
                        lambda i=index: doc.search_page(
                            i, options["pattern"], max_results=maximum + 1 - len(matches), **kwargs
                        ),
                    )
                )
                if len(matches) > maximum:
                    break
    finally:
        native("search index cleanup", doc.clear_search_index)
    if len(matches) > maximum:
        raise JobError(
            "PDF Oxide search result limit exceeded; truncated matches are not published"
        )
    budget = _ContentBudget()
    diagnostics = _diagnostics(doc, source, budget)
    truncated = any(item["native"]["category"] == "operator_cap_exceeded" for item in diagnostics)
    output = {
        "schema_version": 1,
        "source": source,
        "engine": {"name": "pdf-oxide", "version": VERSION},
        "settings": {k: v for k, v in options.items() if k != "password"},
        "semantics_verified": False,
        "ocr": "not_run",
        "truncated": truncated,
        "status": "partial" if truncated else "matches" if matches else "empty",
        "matches": budget.convert(
            [
                {
                    "source_sha256": source["sha256"],
                    "page": value["page"] + 1,
                    "native": value,
                }
                for value in matches
            ]
        ),
        "structured_warnings": diagnostics,
    }
    write_json(request, add, "oxide-search.json", output)
    return ["Search covers native text only; compare extracted matches with the source."], truncated


def _read_regular(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or not 0 < size <= maximum:
            raise JobError("OCR model files must be nonempty regular files within their size limit")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise JobError("OCR model file exceeds its size limit")
    return data


def model_manifest(directory=None, *, verify_files=False):
    """Read operator-provisioned model pins; never trust a request-supplied path or fetch URLs."""
    value = directory if directory is not None else os.environ.get("PDF_MULTITOOL_OXIDE_MODELS")
    if not value:
        raise JobError("PDF Oxide OCR requires a locally provisioned hash-pinned model manifest")
    root = Path(value)
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
        raise JobError(
            "OCR model directory must be an absolute directory without symlink components"
        )
    try:
        manifest = strict_json_loads(_read_regular(root / "manifest.json", 64 * 1024))
    except (OSError, ValueError, TypeError) as exc:
        raise JobError("PDF Oxide OCR model manifest is unavailable or invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {"schema_version", "engine_version", "onnxruntime_version", "language", "files"}
        or manifest["schema_version"] != 1
        or manifest["engine_version"] != VERSION
        or (manifest["onnxruntime_version"] != ONNXRUNTIME_VERSION)
    ):
        raise JobError("Unsupported OCR model manifest schema/version")
    if not isinstance(manifest["language"], str) or not 1 <= len(manifest["language"]) <= 64:
        raise JobError("OCR model language must be a bounded string")
    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != {"detector", "recognizer", "dictionary"}:
        raise JobError("OCR manifest requires detector, recognizer and dictionary")
    total = 0
    for role, item in files.items():
        if not isinstance(item, dict) or set(item) != {
            "file",
            "sha256",
            "size",
            "license",
            "source",
        }:
            raise JobError("OCR model entries require file, SHA-256, size, license and source")
        if not isinstance(item["file"], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", item["file"]
        ):
            raise JobError("OCR model filenames must be local basenames")
        if not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
            raise JobError("OCR model SHA-256 must be a complete lowercase digest")
        maximum = 4 * 1024**2 if role == "dictionary" else 256 * 1024**2
        if type(item["size"]) is not int or not 0 < item["size"] <= maximum:
            raise JobError("OCR model declared size exceeds its limit")
        total += item["size"]
        if any(
            not isinstance(item[k], str) or not 1 <= len(item[k]) <= 1024
            for k in ("license", "source")
        ):
            raise JobError("OCR model license and source attribution are required")
        if verify_files:
            try:
                data = _read_regular(root / item["file"], maximum)
            except OSError as exc:
                raise JobError("OCR model file is unavailable") from exc
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise JobError("OCR model file does not match its pinned size/SHA-256")
    if total > MAX_MODELS_BYTES:
        raise JobError("OCR combined model size exceeds its limit")
    return root, manifest


def ocr_availability():
    """Metadata/configuration gate only; successful loading/inference is a worker check."""
    from .oxide import availability

    base = availability()
    if not base["available"]:
        return base
    try:
        installed = importlib.metadata.version("onnxruntime")
        if installed != ONNXRUNTIME_VERSION:
            raise JobError(f"PDF Oxide OCR requires onnxruntime {ONNXRUNTIME_VERSION}")
        model_manifest()
    except importlib.metadata.PackageNotFoundError:
        return {"available": False, "reason": "Install the optional oxide-ocr extra with uv"}
    except JobError as exc:
        return {"available": False, "reason": str(exc)}
    return {
        "available": True,
        "version": VERSION,
        "reason": "Configured offline; worker must verify hashes, native OCR support and inference",
    }


def _ocr(request, options, add):
    from .oxide_runtime import (
        native,
        open_source,
        require_runtime,
        selected,
        write_bytes,
        write_json,
    )

    module = require_runtime(request)
    status = ocr_availability()
    if not status["available"]:
        raise JobError(status["reason"])
    root, manifest = model_manifest(verify_files=True)
    # Snapshot each pinned model into the private worker directory. Native path-based
    # loaders therefore cannot race a later change to the operator's model files.
    snapshot = Path(request["workdir"]) / "ocr-model-snapshot"
    snapshot.mkdir(mode=0o700)
    paths = {}
    for role, item in manifest["files"].items():
        data = _read_regular(root / item["file"], item["size"])
        if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise JobError("OCR model changed during snapshot verification")
        path = snapshot / role
        path.write_bytes(data)
        paths[role] = str(path)
        del data
    config = native(
        "OCR configuration (requires an OCR-enabled wheel)",
        lambda: module.OcrConfig(
            **{k: options[k] for k in ("det_threshold", "rec_threshold", "num_threads")}
        ),
    )
    engine = native(
        "offline OCR model load",
        lambda: module.OcrEngine(
            paths["detector"], paths["recognizer"], paths["dictionary"], config
        ),
    )
    doc, _, source = open_source(request, options)
    budget = _ContentBudget()
    report = {
        "schema_version": 1,
        "source": source,
        "engine": {"name": "pdf-oxide", "version": VERSION},
        "models": manifest,
        "model_hashes_verified": True,
        "model_authenticity": "Operator-provisioned pins; no source-to-weight attestation claimed",
        "semantics_verified": False,
        "pages": [],
        "settings": {k: v for k, v in options.items() if k != "password"},
    }
    partial = False
    for index in selected(options, source["pages"], maximum=20):
        baseline = budget.convert(
            native(
                f"page {index + 1} native baseline",
                lambda i=index: doc.extract_text(i, extract_tables=False, include_artifacts=True),
            )
        )
        classification = budget.convert(
            native(
                f"page {index + 1} classification",
                lambda i=index: _native_json(doc.classify_page(i)),
            )
        )
        extracted = budget.convert(
            native(f"page {index + 1} OCR", lambda i=index: doc.extract_text_ocr(i, engine))
        )
        page = {
            "page": index + 1,
            "native_page_index": index,
            "source_sha256": source["sha256"],
            "native_text": baseline,
            "ocr_text": extracted,
            "classification": classification,
            "ocr_status": "native_or_ocr_returned" if extracted else "empty",
            "truncated": False,
            "text_source": (
                "extract_text_ocr uses native text on text pages and silently falls back "
                "after some OCR failures; the Python binding does not expose actual provenance"
            ),
            "ocr_inference_verified": False,
        }
        page.update(_page_diagnostics(doc, index))
        partial = (
            partial
            or page["truncated"]
            or (classification.get("kind") in {"scanned", "image_text", "mixed"} and not extracted)
        )
        filename = f"ocr-page-{index + 1:04d}.json"
        write_json(request, add, filename, page)
        write_bytes(request, add, f"ocr-page-{index + 1:04d}.txt", extracted.encode("utf-8"))
        write_bytes(request, add, f"ocr-page-{index + 1:04d}-native.txt", baseline.encode("utf-8"))
        report["pages"].append(
            {"page": index + 1, "file": filename, "ocr_status": page["ocr_status"]}
        )
    report["structured_warnings"] = _diagnostics(doc, source, budget)
    report["partial"] = partial
    write_json(request, add, "oxide-ocr-manifest.json", report)
    # Model files are private execution inputs, never output artifacts.
    for path in snapshot.iterdir():
        path.unlink()
    snapshot.rmdir()
    return ["OCR text requires source comparison, especially numbers, symbols and tables."], partial


def process(request, options, add):
    from .oxide_runtime import require_runtime

    require_runtime(request)
    validate_options(request["operation"], options)
    return {
        "oxide_extract_mask": _mask,
        "oxide_analyze": _analyze,
        "oxide_search": _search,
        "oxide_ocr": _ocr,
    }[request["operation"]](request, options, add)
