"""Capture immutable boundary inputs using the current application; no oracle edits."""

import argparse
import hashlib
import json
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

from pdf_multitool.analysis import (
    _order_baseline_pair,
    _ordered_blocks,
    export_html,
    export_markdown,
    export_text,
)

ROOT = Path(__file__).resolve().parent


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def texts(blocks):
    return [
        ["".join(s["text"] for s in line["spans"]) for line in b.get("lines", [])] for b in blocks
    ]


def main():
    contracts = json.loads((ROOT / "contracts.json").read_text())
    frozen = json.loads((ROOT / "FROZEN.json").read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "observations")
    output = parser.parse_args().output
    output.mkdir(exist_ok=False)
    summary = {}
    for name, case in contracts["cases"].items():
        source = ROOT / "fixtures" / f"{name}.pdf"
        original = source.read_bytes()
        if hashlib.sha256(original).hexdigest() != frozen["pdf_sha256"][name]:
            raise RuntimeError("Frozen input mismatch")
        target = output / name
        target.mkdir()
        with pymupdf.open(source) as doc:
            page = doc[0]
            raw = page.get_text("dict", sort=False)
            local = [_order_baseline_pair(b) for b in raw["blocks"]]
            final = _ordered_blocks(page)
            write_json(
                target / "raw.json",
                {
                    "dict": raw,
                    "rawdict": page.get_text("rawdict", sort=False),
                    "texttrace": page.get_texttrace(),
                },
            )
            write_json(target / "application.json", {"local": local, "ordered": final})
            for exporter, suffix in [
                (export_text, "txt"),
                (export_markdown, "md"),
                (export_html, "html"),
            ]:
                exporter(doc, target / f"export.{suffix}", source.name)
            for dpi in (72, 216):
                page.get_pixmap(dpi=dpi, alpha=False).save(target / f"render-{dpi}.png")
            summary[name] = {
                "policy": case["policy"],
                "raw": texts(raw["blocks"]),
                "local": texts(local),
                "final": texts(final),
                "local_changed": local != raw["blocks"],
                "native_rotation": page.rotation,
                "directions": [line["dir"] for b in raw["blocks"] for line in b.get("lines", [])],
            }
        if source.read_bytes() != original:
            raise RuntimeError("Input bytes changed")
    write_json(output / "summary.json", {"engine": pymupdf.version, "cases": summary})
    names = list(contracts["cases"])
    for dpi, per_sheet, columns in [(72, 15, 3), (216, 4, 2)]:
        width, height = 300 * dpi // 72, 180 * dpi // 72
        for start in range(0, len(names), per_sheet):
            group = names[start : start + per_sheet]
            rows = (len(group) + columns - 1) // columns
            sheet = Image.new("RGB", (columns * width, rows * (height + 25)), "#cccccc")
            draw = ImageDraw.Draw(sheet)
            for index, name in enumerate(group):
                x, y = index % columns * width, index // columns * (height + 25)
                draw.text((x + 8, y + 5), name, fill="black")
                with Image.open(output / name / f"render-{dpi}.png") as rendered:
                    sheet.paste(rendered, (x, y + 25))
            sheet.save(output / f"contact-{dpi}-{start // per_sheet + 1}.png")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
