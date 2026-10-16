"""Public synthetic inputs; author expectations precede PDF Oxide observations."""

import hashlib
import io
import json
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent


def build():
    directory = ROOT / "fixtures"
    directory.mkdir(exist_ok=False)
    contracts = {}
    for tagged in (False, True):
        name = "cleanup_mixed_tagged" if tagged else "cleanup_repeated"
        with pymupdf.open() as doc:
            for number in (1, 2):
                page = doc.new_page(width=600, height=800)
                page.insert_text((60, 40), "Meaningful repeated table heading 2026")
                if tagged and number == 1:
                    xref = page.get_contents()[0]
                    doc.update_stream(
                        xref,
                        b"/Artifact <</Type /Pagination /Subtype /Header>> BDC\n"
                        + doc.xref_stream(xref)
                        + b"\nEMC",
                    )
                page.insert_text((60, 300), "Meaningful repeated table heading 2026")
                page.insert_text((60, 330), f"Body record {number} 012 0.15")
                page.insert_text((60, 770), "Citation 7: repeated supporting reference")
            doc.save(directory / f"{name}.pdf", no_new_id=True)
        contracts[name] = {
            "pages": 2,
            "heading_occurrences_per_page": 2,
            "citation_occurrences_per_page": 1,
            "meaningful_text_must_survive_in_original_copy": True,
            "cleanup_may_remove_meaningful_content": True,
            "tagged_header_pages": [1] if tagged else [],
        }
    for name in ("images_rgba_repeated", "images_cmyk", "images_inline", "images_malformed"):
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=800)
            page.insert_text((60, 60), f"Synthetic {name} 001 12.50")
            if name == "images_inline":
                stream = b"q 160 0 0 160 60 440 cm BI /W 16 /H 16 /CS /RGB /BPC 8 /F /AHx ID\n"
                stream += (b"ff0000" * 128 + b"0000ff" * 128) + b">\nEI Q\n"
                xref = doc.get_new_xref()
                doc.update_object(xref, "<<>>")
                doc.update_stream(xref, stream)
                current = " ".join(f"{value} 0 R" for value in page.get_contents())
                doc.xref_set_key(page.xref, "Contents", f"[{current} {xref} 0 R]")
            else:
                image = Image.new(
                    "CMYK" if name == "images_cmyk" else "RGBA",
                    (64, 48),
                    (0, 255, 255, 0) if name == "images_cmyk" else (255, 0, 0, 80),
                )
                ImageDraw.Draw(image).rectangle(
                    (10, 10, 40, 30),
                    fill=(255, 0, 0, 0) if image.mode == "CMYK" else (0, 0, 255, 255),
                )
                data = io.BytesIO()
                image.save(data, format="JPEG" if image.mode == "CMYK" else "PNG")
                xref = page.insert_image(pymupdf.Rect(60, 150, 316, 342), stream=data.getvalue())
                if name == "images_rgba_repeated":
                    page.insert_image(pymupdf.Rect(60, 400, 316, 592), xref=xref)
                if name == "images_malformed":
                    doc.update_stream(xref, b"deliberately invalid compressed image")
                    doc.xref_set_key(xref, "Filter", "/FlateDecode")
            doc.save(directory / f"{name}.pdf", no_new_id=True)
        contracts[name] = {
            "authored_placements": 2 if name == "images_rgba_repeated" else 1,
            "malformed": name == "images_malformed",
            "expect_complete_visible_image_recovery": name != "images_malformed",
            "pixel_color_alpha_fidelity": "requires source/output visual review",
        }
    (ROOT / "contracts.json").write_text(json.dumps(contracts, indent=2) + "\n")
    files = [Path(__file__), ROOT / "contracts.json", *sorted(directory.glob("*.pdf"))]
    frozen = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files
    }
    (ROOT / "FROZEN.json").write_text(
        json.dumps({"phase": "before_new_fixture_native_capture", "files": frozen}, indent=2) + "\n"
    )


if __name__ == "__main__":
    build()
