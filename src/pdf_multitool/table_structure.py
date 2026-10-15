"""Bounded native cell geometry, with no automatic borderless semantic classifier.

PyMuPDF's table finder supplies boundaries, not replacement text. Every emitted
cell is assembled from uniquely assigned source line occurrences. Ambiguous or
unsupported candidates leave the original content path intact.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import math
import unicodedata
from bisect import bisect_right

import pymupdf

MAX_LINES = 5_000
MAX_PATHS = 10_000
MAX_TABLES = 200
MAX_CELLS = 50_000
MAX_CHARACTERS = 500_000
EPS = 0.01


def new_budget():
    return {"tables": MAX_TABLES, "cells": MAX_CELLS, "characters": MAX_CHARACTERS}


def _text(line):
    # The caller supplies the same source-line text function used by its exports.
    return "".join(span.get("text", "") for span in line.get("spans", []))


def _box(value):
    if len(value) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in value):
        raise ValueError("nonfinite cell or source geometry")
    if value[0] >= value[2] or value[1] >= value[3]:
        raise ValueError("nonpositive cell or source geometry")
    return list(value)


def _intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _within(a, b):
    return all(a[i] >= b[i] - EPS for i in (0, 1)) and all(a[i] <= b[i] + EPS for i in (2, 3))


def _native_grid(table):
    rows = [[_box(cell) if cell is not None else None for cell in row.cells] for row in table.rows]
    if len(rows) < 2 or not rows or len(rows[0]) < 2:
        raise ValueError("fewer than two rows or columns")
    if len(rows) * len(rows[0]) > MAX_CELLS:
        raise ValueError("cell budget exceeded")
    if any(len(row) != len(rows[0]) or any(cell is None for cell in row) for row in rows):
        raise ValueError("merged or incomplete native cell grid")
    xs = [cell[0] for cell in rows[0]] + [rows[0][-1][2]]
    ys = [row[0][1] for row in rows] + [rows[-1][0][3]]
    for r, row in enumerate(rows):
        for c, box in enumerate(row):
            expected = [xs[c], ys[r], xs[c + 1], ys[r + 1]]
            if any(abs(a - b) > EPS for a, b in zip(box, expected, strict=True)):
                raise ValueError("nonrectangular or spanning cell geometry")
    return xs, ys


def _candidate(table, source, strategy, line_text):
    xs, ys = _native_grid(table)
    bounds = [xs[0], ys[0], xs[-1], ys[-1]]
    members = []
    for index, line in source:
        box = _box(line["bbox"])
        center_y = (box[1] + box[3]) / 2
        if strategy == "text":
            relevant = (
                bounds[1] <= center_y <= bounds[3] and box[0] < bounds[2] and box[2] > bounds[0]
            )
        else:
            relevant = _intersects(box, bounds)
        if not relevant:
            continue
        if tuple(line.get("dir", ())) != (1, 0) or line.get("wmode") != 0:
            raise ValueError("table contains non-horizontal text")
        if any(unicodedata.bidirectional(c) in {"R", "AL", "AN"} for c in line_text(line)):
            raise ValueError("table contains bidirectional text")
        if box[0] < bounds[0] - EPS or box[2] > bounds[2] + EPS:
            raise ValueError("candidate clips a source line")
        members.append((index, line))
    if not members:
        raise ValueError("no complete source lines in candidate")
    if strategy == "text":
        # Text-finder edges often split each repeated word into a column. Merge
        # boundaries that cross a complete source line, without inspecting its text.
        xs = (
            [xs[0]]
            + [
                x
                for x in xs[1:-1]
                if not any(line["bbox"][0] + EPS < x < line["bbox"][2] - EPS for _, line in members)
            ]
            + [xs[-1]]
        )
        if len(xs) < 3:
            raise ValueError("text alignment does not define two complete-line columns")
    cells = [[[] for _ in xs[:-1]] for _ in ys[:-1]]
    for index, line in members:
        box = line["bbox"]
        center_y = (box[1] + box[3]) / 2
        r = bisect_right(ys, center_y) - 1
        c = bisect_right(xs, (box[0] + box[2]) / 2) - 1
        if not (0 <= r < len(ys) - 1 and 0 <= c < len(xs) - 1):
            raise ValueError("source line has no cell")
        cell = [xs[c], ys[r], xs[c + 1], ys[r + 1]]
        inside = (
            _within(box, cell)
            if strategy != "text"
            else (cell[0] - EPS <= box[0] and box[2] <= cell[2] + EPS)
        )
        if not inside:
            raise ValueError("source line crosses cell boundaries or has no unique cell")
        cells[r][c].append((index, line))
    omitted = []
    if strategy == "text":
        omitted = [r for r, row in enumerate(cells) if not any(row)]
    kept = [r for r in range(len(cells)) if r not in omitted]
    if len(kept) < 2:
        raise ValueError("fewer than two populated text bands")
    public_cells, internal_cells = [], []
    for r in kept:
        public_row, internal_row = [], []
        for c, assigned in enumerate(cells[r]):
            assigned.sort(key=lambda item: (item[1]["bbox"][1], item[1]["bbox"][0], item[0]))
            lines = [line for _, line in assigned]
            public_row.append(
                {
                    "bbox": [xs[c], ys[r], xs[c + 1], ys[r + 1]],
                    "text": "\n".join(line_text(line) for line in lines),
                    "source_line_ids": [index for index, _ in assigned],
                }
            )
            internal_row.append(lines)
        public_cells.append(public_row)
        internal_cells.append(internal_row)
    # Close text bands admit wrapped-cell and separate-row interpretations.
    occupied_y = [
        min(line["bbox"][1] for cell in cells[r] for _, line in cell) for r in kept if any(cells[r])
    ]
    line_height = max(line["bbox"][3] - line["bbox"][1] for _, line in members)
    close_bands = strategy == "text" and any(
        b - a < 1.5 * line_height for a, b in itertools.pairwise(occupied_y)
    )
    return {
        "bbox": bounds,
        "strategy": strategy,
        "cells": public_cells,
        "rows": [[cell["text"] for cell in row] for row in public_cells],
        "source_line_ids": [index for index, _ in members],
        "source_line_occurrences": len(members),
        "spacer_bands_omitted": omitted,
        "ambiguous_row_grouping": close_bands,
        "cell_boundaries_from_rules": strategy != "text",
        "semantics_verified": False,
        "header_inferred": False,
        "_cell_lines": internal_cells,
    }


def project_tables(page, blocks, policy, line_text=_text, allow_projection=True, budget=None):
    """Return replacement blocks and a serializable page-level decision record."""
    report = {
        "page": page.number + 1,
        "engine": "PyMuPDF",
        "version": pymupdf.VersionBind,
        "policy": policy,
        "tables": [],
        "rejected": [],
        "semantics_verified": False,
    }
    source = [
        (i, line)
        for i, line in enumerate(
            line for block in blocks if block.get("type") == 0 for line in block.get("lines", [])
        )
        if line_text(line).strip()
    ]
    if budget is None:
        budget = new_budget()
    budget["characters"] -= sum(len(line_text(line)) for _, line in source)
    if budget["characters"] < 0:
        report["skipped"] = "document character budget exceeded"
        return blocks, report
    if page.rotation:
        report["skipped"] = "rotated page"
        return blocks, report
    if len(source) < 4:
        return blocks, report
    if len(source) > MAX_LINES:
        report["skipped"] = "source-line budget exceeded"
        return blocks, report
    consumed, replacements, ruled_bounds = set(), [], []
    try:
        paths = page.get_drawings()
        if len(paths) > MAX_PATHS:
            report["skipped"] = "drawing-path budget exceeded"
            return blocks, report
        for strategy in ("lines_strict", "text"):
            # Keep dependency recommendations off the worker's JSON stdout channel.
            with contextlib.redirect_stdout(io.StringIO()):
                finder = page.find_tables(strategy=strategy, min_words_vertical=2, paths=paths)
            if len(finder.tables) > budget["tables"]:
                raise ValueError("table-count budget exceeded")
            for native in finder.tables:
                if strategy == "text" and any(
                    _intersects(native.bbox, box) for box in ruled_bounds
                ):
                    continue
                try:
                    item = _candidate(native, source, strategy, line_text)
                    if any(
                        _intersects(item["bbox"], b["bbox"]) for b in blocks if b.get("type") == 1
                    ):
                        raise ValueError("candidate overlaps an image occurrence")
                    if consumed.intersection(item["source_line_ids"]):
                        raise ValueError("candidate overlaps an already assigned source occurrence")
                except (ValueError, TypeError, KeyError, IndexError) as exc:
                    report["rejected"].append({"strategy": strategy, "reason": str(exc)})
                    continue
                if strategy != "text":
                    ruled_bounds.append(item["bbox"])
                budget["tables"] -= 1
                budget["cells"] -= sum(len(row) for row in item["cells"])
                if budget["cells"] < 0:
                    raise ValueError("document cell budget exceeded")
                promote = (
                    allow_projection
                    and policy != "columns"
                    and (
                        strategy != "text"
                        or (policy == "rows" and not item["ambiguous_row_grouping"])
                    )
                )
                item["disposition"] = (
                    "text_path_for_margin_filter"
                    if not allow_projection
                    else "structured_ruled_grid"
                    if promote and strategy != "text"
                    else "user_selected_spatial_bands"
                    if promote
                    else "ambiguous_borderless"
                    if strategy == "text"
                    else "explicit_column_order"
                )
                item["emitted_as_table"] = promote
                item["id"] = f"page-{page.number + 1}-grid-{len(report['tables']) + 1}"
                cell_lines = item.pop("_cell_lines")
                report["tables"].append(item)
                if promote:
                    consumed.update(item["source_line_ids"])
                    replacements.append(
                        {
                            "type": "table",
                            "bbox": item["bbox"],
                            "table": item,
                            "cell_lines": cell_lines,
                            "lines": [line for row in cell_lines for cell in row for line in cell],
                        }
                    )
    except (ValueError, RuntimeError, TypeError) as exc:
        # A detector failure cannot erase or duplicate source content. Discard
        # every partial projection on this page and retain the original path.
        report["skipped"] = f"native table detection failed: {type(exc).__name__}"
        report["tables"] = []
        return blocks, report
    if not consumed:
        return blocks, report
    consumed_objects = {id(line) for index, line in source if index in consumed}
    result = []
    for block in blocks:
        if block.get("type") != 0:
            result.append(block)
            continue
        remaining = [line for line in block.get("lines", []) if id(line) not in consumed_objects]
        if remaining:
            bounds = [line["bbox"] for line in remaining]
            result.append(
                {
                    **block,
                    "lines": remaining,
                    "bbox": [
                        min(b[0] for b in bounds),
                        min(b[1] for b in bounds),
                        max(b[2] for b in bounds),
                        max(b[3] for b in bounds),
                    ],
                }
            )
    return result + replacements, report
