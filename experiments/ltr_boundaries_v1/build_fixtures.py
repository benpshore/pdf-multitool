"""Author synthetic PDFs before observing parser outputs; no extraction here."""

import hashlib
import io
import json
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parent
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def build(case):
    output = io.BytesIO()
    canvas = Canvas(output, pagesize=(300, 180), invariant=1, pageCompression=0)
    canvas.setTitle("Synthetic boundary qualification")
    if case.get("rotation"):
        canvas.translate(150, 90)
        canvas.rotate(case["rotation"])
    else:
        canvas.translate(40, 110)
    font = case.get("font", "Courier")
    left, right = case.get("words", ["ALPHA", "BETA"])
    size = 10
    x_right = pdfmetrics.stringWidth(left, font, size) + case.get("gap", 6)
    canvas.setFont(font, case.get("right_size", size))
    canvas.drawString(x_right, case.get("offset", 0), right)
    canvas.setFont(font, size)
    canvas.drawString(0, 0, left)
    canvas.showPage()
    canvas.save()
    return output.getvalue()


def main():
    pdfmetrics.registerFont(TTFont("BoundaryDejaVu", str(FONT)))
    cases = json.loads((ROOT / "contracts.json").read_text())["cases"]
    hashes = {}
    for name, case in cases.items():
        data = build(case)
        path = ROOT / "fixtures" / f"{name}.pdf"
        if path.exists():
            if path.read_bytes() != data:
                raise RuntimeError(f"Refusing to overwrite different frozen fixture: {name}")
        else:
            path.write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    frozen = {
        "base": "1bfbe98740e75b534ce6c9ef87a88aefbf39742d",
        "font_sha256": hashlib.sha256(FONT.read_bytes()).hexdigest(),
        "contracts_sha256": hashlib.sha256((ROOT / "contracts.json").read_bytes()).hexdigest(),
        "builder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "pdf_sha256": hashes,
    }
    target = ROOT / "FROZEN.json"
    value = json.dumps(frozen, indent=2) + "\n"
    if target.exists() and target.read_text() != value:
        raise RuntimeError("Refusing to replace frozen manifest")
    target.write_text(value)
    print(f"{len(hashes)} bounded synthetic PDFs frozen/verified")


if __name__ == "__main__":
    main()
