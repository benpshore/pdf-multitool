"""Synthetic documents only. No user corpus, bundled fonts or external assets."""

from pathlib import Path

import pymupdf


def document(path: Path, pages: int = 3) -> Path:
    with pymupdf.open() as pdf:
        for n in range(pages):
            page = pdf.new_page(width=400, height=500)
            page.insert_text((40, 50), f"Synthetic title {n + 1}", fontsize=20)
            page.insert_text((40, 100), f"Page {n + 1}: visible test content. 12345", fontsize=12)
            page.draw_rect(
                pymupdf.Rect(40, 130, 140, 180), color=(0.2, 0.4, 0.8), fill=(0.9, 0.95, 1)
            )
        pdf.save(path)
    return path


def scanned(path: Path) -> Path:
    with pymupdf.open() as image_doc:
        page = image_doc.new_page(width=400, height=500)
        page.insert_text((40, 100), "Synthetic scan requires OCR", fontsize=16)
        image = page.get_pixmap().tobytes("png")
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=500)
        page.insert_image(page.rect, stream=image)
        pdf.save(path)
    return path
