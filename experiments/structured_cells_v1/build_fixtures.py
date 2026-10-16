"""Freeze authored cell grids before calling any parser or product implementation."""

import hashlib
import json
from pathlib import Path

from experiments.representation_v1.build_fixtures import pdf, show

ROOT = Path(__file__).resolve().parent


def make(grid, heights, ruled, emission="rows", surround=False):
    xs = [40, 180, 430, 570] if len(grid[0]) == 3 else [40, 300, 570]
    ys = [90]
    for height in heights:
        ys.append(ys[-1] + height)
    placements = []
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            for n, text in enumerate(cell.split("\n")):
                if text:
                    placements.append((r, c, n, text, xs[c] + 8, 800 - ys[r] - 16 - n * 13))
    if emission == "columns":
        placements.sort(key=lambda p: (p[1], p[0], p[2]))
    elif emission == "reverse":
        placements.reverse()
    content = ""
    if surround:
        content += show("Before the grid", 40, 750)
    if ruled:
        content += "0.5 w\n"
        for x in xs:
            content += f"{x} {800 - ys[0]} m {x} {800 - ys[-1]} l S\n"
        for y in ys:
            content += f"{xs[0]} {800 - y} m {xs[-1]} {800 - y} l S\n"
    content += "".join(show(text, x, y) for _, _, _, text, x, y in placements)
    if surround:
        content += show("After the grid", 40, 800 - ys[-1] - 40)
    return pdf(content), xs, ys


def fixtures():
    designs = {
        "numeric": (
            [
                ["Code", "Value", "Rate"],
                ["0007", "1,234.50", "12.5%"],
                ["0042", "-90.00", "0.0%"],
                ["0100", "0.00", "100%"],
            ],
            [30, 30, 30, 30],
        ),
        "missing": (
            [["Key", "Amount", "Tax"], ["012", "", "0.15"], ["036", "2.00", ""]],
            [30, 30, 30],
        ),
        "wrapped": (
            [
                ["ID", "Description", "Amount"],
                ["001", "First line\ncontinued description", "10.25"],
                ["002", "Short", "-2.00"],
            ],
            [30, 45, 30],
        ),
        "empty_row": (
            [["A", "B"], ["", ""], ["001", "0.00"], ["002", "-3.25"]],
            [30, 30, 30, 30],
        ),
    }
    output, contracts = {}, {}
    for family, (grid, heights) in designs.items():
        for ruled in (True, False):
            for emission in ("rows", "columns", "reverse"):
                name = f"{family}_{'ruled' if ruled else 'borderless'}_{emission}"
                data, xs, ys = make(grid, heights, ruled, emission, surround=ruled)
                output[name] = data
                contracts[name] = {
                    "authored_cells": grid,
                    "authored_row_bounds": ys,
                    "authored_column_bounds": xs,
                    "ruled": ruled,
                    "emission": emission,
                    "expected_auto_disposition": "structured_grid" if ruled else "ambiguous",
                    "author_intent": "table_rows",
                    "surrounding_text": ["Before the grid", "After the grid"] if ruled else [],
                }
    prose = [
        ["North 10\nthen south 30", "East 20\nthen west 40"],
        ["Upper 50\nthen lower 60", "Earlier 70\nthen later 80"],
        ["First 90\nthen last 100", "Next 110\nthen final 120"],
    ]
    for emission in ("rows", "columns", "reverse"):
        name = "wrapped_numeric_prose_" + emission
        output[name], xs, ys = make(prose, [45, 45, 45], False, emission)
        contracts[name] = {
            "authored_cells": None,
            "authored_column_text": ["\n".join(row[c] for row in prose) for c in (0, 1)],
            "ruled": False,
            "emission": emission,
            "expected_auto_disposition": "ambiguous",
            "author_intent": "prose_columns",
        }
    return output, contracts


def main():
    generated, expected = fixtures()
    contracts = json.dumps(expected, indent=2) + "\n"
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in generated.items()}
    frozen = {
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "contracts_sha256": hashlib.sha256(contracts.encode()).hexdigest(),
        "pdf_sha256": hashes,
        "scope": "Local authored synthetic cell grids; not an independent semantic benchmark.",
    }
    if (ROOT / "FROZEN.json").exists():
        if json.loads((ROOT / "FROZEN.json").read_text()) != frozen:
            raise RuntimeError("Frozen generator or PDF recipe changed")
        if (ROOT / "contracts.json").read_text() != contracts:
            raise RuntimeError("Frozen contracts changed")
        for name, data in generated.items():
            if (ROOT / "fixtures" / (name + ".pdf")).read_bytes() != data:
                raise RuntimeError(f"Frozen PDF changed: {name}")
    else:
        (ROOT / "fixtures").mkdir()
        for name, data in generated.items():
            (ROOT / "fixtures" / (name + ".pdf")).write_bytes(data)
        (ROOT / "contracts.json").write_text(contracts)
        (ROOT / "FROZEN.json").write_text(json.dumps(frozen, indent=2) + "\n")
    print(f"Verified {len(generated)} frozen PDFs without calling a parser or the product")


if __name__ == "__main__":
    main()
