"""Freeze independent row/column contracts before measuring product behavior.

Uses only the standard library and the unchanged deterministic PDF byte writer.
Do not regenerate the frozen oracle in response to an implementation failure.
"""

import argparse
import hashlib
import json
from pathlib import Path

from experiments.representation_v1.build_fixtures import pdf, show

ROOT = Path(__file__).resolve().parent


def literal(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def fixtures():
    grids = {
        "numeric_cells": [
            ["0007", "1,234.50", "12.5%"],
            ["0042", "-90.00", "0.0%"],
            ["0100", "0.00", "100%"],
        ],
        "long_records": [
            ["Invoice number 104 completed", "Payment amount USD 120.50"],
            ["Invoice number 205 pending", "Payment amount USD 0.00"],
            ["Invoice number 306 disputed", "Payment amount USD -75.25"],
        ],
        "short_column_prose": [["First", "Once"], ["Next", "Then"], ["Last", "Finally"]],
        "missing_cells": [["ID", "Amount", "Tax"], ["012", "", "0.15"], ["036", "2.00", ""]],
        "ambiguous_grid": [["North", "East"], ["South", "West"], ["Upper", "Lower"]],
    }
    result = {}
    for name, cells in grids.items():
        x_values = (40, 240, 440) if len(cells[0]) == 3 else (40, 330)
        placements = [
            (text, x_values[col], 740 - row * 30)
            for row, values in enumerate(cells)
            for col, text in enumerate(values)
            if text
        ]
        row_text = " ".join(text for text, _, _ in placements)
        columns = sorted(placements, key=lambda value: (value[1], -value[2]))
        column_text = " ".join(text for text, _, _ in columns)
        for emission, ordered in (
            ("rows", placements),
            ("columns", columns),
            ("reverse", placements[::-1]),
        ):
            key = name + "_" + emission
            result[key] = {
                "pdf": pdf("".join(show(literal(text), x, y) for text, x, y in ordered)),
                "contract": {
                    "grid": cells,
                    "row_text": row_text,
                    "column_text": column_text,
                    "intent": "columns"
                    if name == "short_column_prose"
                    else "both_possible"
                    if name == "ambiguous_grid"
                    else "rows",
                    "emission": emission,
                },
            }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", action="store_true")
    args = parser.parse_args()
    generated = fixtures()
    contracts = {name: row["contract"] for name, row in generated.items()}
    contracts["protocol"] = {
        "frozen_before_product_measurement": True,
        "identical_input_ambiguity": "Each ambiguous_grid PDF admits both author intents. No automatic function of its bytes can satisfy both different ordered-text oracles.",
        "missing_cells": "Order of populated cells is checked; a flat text sequence does not encode empty-cell identity or validate table semantics.",
        "source_expectations_unchanged": "The existing eleven representation expectations and thirteen boundary contracts remain immutable.",
        "scope": "Horizontal unrotated synthetic text, explicit reading policy; not a universal table/prose classifier.",
    }
    serialized = json.dumps(contracts, indent=2, ensure_ascii=False) + "\n"
    hashes = {
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "contracts_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "pdf_sha256": {
            name: hashlib.sha256(row["pdf"]).hexdigest() for name, row in generated.items()
        },
    }
    frozen = ROOT / "FROZEN.json"
    if args.freeze:
        if frozen.exists():
            raise RuntimeError("Already frozen; never overwrite these expectations")
        (ROOT / "fixtures").mkdir()
        (ROOT / "contracts.json").write_text(serialized)
        for name, row in generated.items():
            (ROOT / "fixtures" / (name + ".pdf")).write_bytes(row["pdf"])
        frozen.write_text(json.dumps(hashes, indent=2) + "\n")
    else:
        if (
            json.loads(frozen.read_text()) != hashes
            or (ROOT / "contracts.json").read_text() != serialized
        ):
            raise RuntimeError("Frozen generator or contracts changed")
        for name, row in generated.items():
            if (ROOT / "fixtures" / (name + ".pdf")).read_bytes() != row["pdf"]:
                raise RuntimeError("Frozen PDF changed: " + name)
    print(
        f"Verified {len(generated)} independent PDFs without importing the product or a PDF parser"
    )


if __name__ == "__main__":
    main()
