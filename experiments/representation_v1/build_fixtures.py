"""Create deterministic synthetic PDFs and freeze expectations before measurement.

This module uses only the standard library and does not import PDF parsers or the app.
Run once with --freeze; use --check thereafter to verify the frozen bytes.
"""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def show(text, x=40, y=740):
    return f"BT /F1 10 Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET\n"


def pdf(content, form=None):
    resources = "/Font << /F1 4 0 R >>"
    if form is not None:
        resources += " /XObject << /Fm 6 0 R >>"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] "
            f"/Resources << {resources} >> /Contents 5 0 R >>"
        ).encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    ]
    raw = content.encode("ascii")
    objects.append(f"<< /Length {len(raw)} >>\nstream\n".encode() + raw + b"endstream")
    if form is not None:
        raw = form.encode("ascii")
        header = (
            "<< /Type /XObject /Subtype /Form /BBox [0 -5 100 15] "
            "/Resources << /Font << /F1 4 0 R >> >> "
            f"/Length {len(raw)} >>\nstream\n"
        )
        objects.append(header.encode() + raw + b"endstream")
    data = b"%PDF-1.4\n%Synthetic deterministic fixture\n"
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    data += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return data


def fixtures():
    # Courier has 600-unit glyph widths; at 10pt every glyph advances exactly 6pt.
    items = {
        "space_explicit": pdf(show("ALPHA BETA")),
        "space_tj": pdf("BT /F1 10 Tf 1 0 0 1 40 740 Tm [(ALPHA) -600 (BETA)] TJ ET\n"),
        "operator_chunks": pdf(
            "BT /F1 10 Tf 1 0 0 1 40 740 Tm (AL) Tj (PHA) Tj ( ) Tj (BE) Tj (TA) Tj ET\n"
        ),
        "objects_forward": pdf(show("ALPHA") + show("BETA", 76)),
        "objects_reverse": pdf(show("BETA", 76) + show("ALPHA")),
        "form_repeated": pdf(
            "q 1 0 0 1 40 740 cm /Fm Do Q\nq 1 0 0 1 40 710 cm /Fm Do Q\n",
            form=show("STAMP", 0, 0),
        ),
        "form_expanded": pdf(show("STAMP") + show("STAMP", 40, 710)),
    }
    left = [f"L{i} Left column sentence {i}." for i in range(1, 4)]
    right = [f"R{i} Right column sentence {i}." for i in range(1, 4)]
    for family, first, second in (
        ("prose", left, right),
        ("table", [f"A{i}" for i in range(1, 4)], [f"B{i}" for i in range(1, 4)]),
    ):
        placements = [
            (text, x, 740 - i * 30)
            for i in range(3)
            for text, x in ((first[i], 40), (second[i], 330))
        ]
        items[f"{family}_row_operators"] = pdf("".join(show(*p) for p in placements))
        items[f"{family}_column_operators"] = pdf(
            "".join(show(*p) for p in placements[::2] + placements[1::2])
        )
    return items


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--freeze", action="store_true")
    modes.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generated = fixtures()
    expected = json.loads((ROOT / "expectations.json").read_text())
    if set(generated) != set(expected["fixtures"]):
        raise RuntimeError("Fixture/expectation inventory mismatch")
    freeze = {
        "schema_version": 1,
        "base_commit": "4ed4f914fe11024cb8771734afbda6747f6521e3",
        "generator_sha256": digest(Path(__file__).read_bytes()),
        "expectations_sha256": digest((ROOT / "expectations.json").read_bytes()),
        "pdf_sha256": {name: digest(data) for name, data in generated.items()},
    }
    target = ROOT / "FROZEN.json"
    if args.freeze:
        if target.exists():
            raise RuntimeError("Already frozen; do not overwrite this experiment")
        (ROOT / "fixtures").mkdir(exist_ok=True)
        for name, data in generated.items():
            (ROOT / "fixtures" / f"{name}.pdf").write_bytes(data)
        target.write_text(json.dumps(freeze, indent=2) + "\n")
    else:
        if json.loads(target.read_text()) != freeze:
            raise RuntimeError("Frozen generator, expectations or PDF recipe changed")
        for name, data in generated.items():
            if (ROOT / "fixtures" / f"{name}.pdf").read_bytes() != data:
                raise RuntimeError(f"Frozen PDF changed: {name}")
    print(f"Verified {len(generated)} synthetic PDFs; no parser/application analysis invoked")


if __name__ == "__main__":
    main()
