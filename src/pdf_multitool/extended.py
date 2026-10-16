"""Bounded toolkit adapters. Only the disposable worker calls these functions."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from itertools import pairwise
from pathlib import Path

from .core import JobError, parse_pages, safe_name


def _json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )


def _selected(options, count, maximum=100):
    selected = parse_pages(options.get("pages", ""), count)
    if len(selected) > maximum:
        raise JobError(f"This operation is limited to {maximum} selected pages")
    return selected


def _font(text):
    import pymupdf

    font = pymupdf.Font("cjk")
    if any(not font.has_glyph(ord(c)) for c in text if c not in "\n\r\t"):
        raise JobError("Text contains a character without a glyph in the bundled font")
    return font


def _lines(text, font, size, width):
    """Wrap measured glyph advances, preserving explicit line breaks and every character."""
    lines = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n"):
        line, used = "", 0.0
        for char in paragraph:
            advance = font.text_length(char, fontsize=size)
            if line and used + advance > width:
                lines.append(line)
                line, used = "", 0.0
            line += char
            used += advance
        lines.append(line)
    return lines


def _create(options, out):
    import pymupdf

    text, size, margin = options["content"], options["font_size"], options["margin"]
    font = _font(text)
    width, height = pymupdf.paper_size(options["paper"])
    lines = _lines(text, font, size, width - 2 * margin)
    per_page = int((height - 2 * margin) / (size * 1.5))
    if per_page < 1 or len(lines) > per_page * 100:
        raise JobError("Text layout exceeds the 100-page creation limit")
    with pymupdf.open() as doc:
        for start in range(0, len(lines), per_page):
            page = doc.new_page(width=width, height=height)
            page.insert_font(fontname="workbench", fontbuffer=font.buffer)
            for offset, line in enumerate(lines[start : start + per_page]):
                page.insert_text(
                    (margin, margin + size + offset * size * 1.5),
                    line,
                    fontsize=size,
                    fontname="workbench",
                )
        doc.subset_fonts()
        doc.save(out, garbage=4, deflate=True)
    return []


def _paginate(doc, options, out):
    import pymupdf

    header, footer = options["header"], options["footer"]
    if not header and not footer:
        raise JobError("Enter a header or footer")
    # No format() or user-controlled expression evaluation.
    font = _font(header + footer + "0123456789")
    size, margin = options["font_size"], options["margin"]
    for index in _selected(options, len(doc), 2000):
        page = doc[index]
        if page.rotation:
            raise JobError("Headers and footers currently require unrotated pages")
        rect = page.rect
        if 2 * margin + size * 3 >= min(rect.width, rect.height):
            raise JobError("Page is too small for the requested margins")
        existing = {entry[4] for entry in page.get_fonts()}
        fontname = "workbench_header"
        while fontname in existing:
            fontname += "_"
        page.insert_font(fontname=fontname, fontbuffer=font.buffer)
        for template, y in ((header, margin), (footer, rect.height - margin)):
            text = template.replace("{page}", str(index + 1)).replace("{pages}", str(len(doc)))
            if (
                "\n" in text
                or "\r" in text
                or font.text_length(text, fontsize=size) > rect.width - 2 * margin
            ):
                raise JobError("Header or footer does not fit on one line inside the margins")
            page.insert_text(pymupdf.Point(margin, y), text, fontname=fontname, fontsize=size)
    doc.subset_fonts()
    doc.save(out, garbage=3, deflate=True)
    return [
        (
            "Headers and footers can overlap existing content; review each page. "
            "Signatures are invalidated."
        )
    ]


def _plumber(path, doc, options, workdir, operation, add):
    import pdfplumber

    selected = _selected(options, len(doc))
    pages, total_chars, total_cells, table_count, text_parts = [], 0, 0, 0, []
    with pdfplumber.open(path, password=options.get("password") or None) as pdf:
        for index in selected:
            page = pdf.pages[index]
            total_chars += len(page.chars)
            if total_chars > 500_000:
                raise JobError("Extraction exceeds the 500,000-character limit")
            if operation == "geometry":
                actual = (
                    page.dedupe_chars(tolerance=options["tolerance"])
                    if options["dedupe_chars"]
                    else page
                )
                text = actual.extract_text() or ""
                text_parts.append(f"--- Page {index + 1} ---\n{text}")
                keys = ("text", "x0", "top", "x1", "bottom", "fontname", "size", "upright")
                pages.append(
                    {
                        "page": index + 1,
                        "width": page.width,
                        "height": page.height,
                        "text": text,
                        "removed_duplicate_characters": len(page.chars) - len(actual.chars),
                        "characters": [{k: c.get(k) for k in keys} for c in actual.chars],
                        "words": actual.extract_words(),
                    }
                )
            else:
                settings = {
                    "vertical_strategy": options["strategy"],
                    "horizontal_strategy": options["strategy"],
                    "snap_tolerance": options["tolerance"],
                    "join_tolerance": options["tolerance"],
                }
                tables = []
                for number, table in enumerate(page.find_tables(settings), 1):
                    rows = table.extract()
                    total_cells += sum(len(row) for row in rows)
                    table_count += 1
                    if total_cells > 50_000 or table_count > 200:
                        raise JobError("Table extraction exceeds its cell or table limit")
                    name = f"page-{index + 1:04d}-table-{number:03d}.csv"
                    with (workdir / name).open("w", newline="", encoding="utf-8") as file:
                        writer = csv.writer(file)
                        # Spreadsheet formulas are inert in JSON; protect CSV consumers too.
                        writer.writerows(
                            [
                                [
                                    (
                                        "'" + cell
                                        if cell.lstrip().startswith(("=", "+", "-", "@"))
                                        else cell
                                    )
                                    if isinstance(cell, str)
                                    else ""
                                    for cell in row
                                ]
                                for row in rows
                            ]
                        )
                    add(workdir / name, name)
                    tables.append({"bbox": list(table.bbox), "rows": rows, "csv": name})
                pages.append({"page": index + 1, "tables": tables})
            page.close()
    report = {
        "engine": "pdfplumber",
        "version": pdfplumber.__version__,
        "pages": pages,
        "status": "partial" if operation == "tables" and not table_count else "completed",
        "coordinates": "PDF points, origin at top-left",
        "semantics_verified": False,
    }
    _json(workdir / "extraction.json", report)
    add(workdir / "extraction.json")
    if operation == "geometry":
        (workdir / "text.txt").write_text("\n\n".join(text_parts), encoding="utf-8")
        add(workdir / "text.txt")
    warnings = [
        (
            "Extracted text and table relationships are candidates, not verified "
            "semantics. CSV formula-leading cells are escaped; JSON preserves their "
            "original text."
        )
    ]
    if report["status"] == "partial":
        warnings.append(
            "No tables detected with the chosen strategy. Try a different "
            "strategy or inspect the original."
        )
    return warnings, report["status"] == "partial"


def _pike(path, options, workdir, operation, limits, add):
    import pikepdf

    warnings = []
    # qpdf's native limits stay enabled; never disable them for malformed inputs.
    with pikepdf.open(path, password=options.get("password", "")) as pdf:
        if not 1 <= len(pdf.pages) <= limits["max_pages"]:
            raise JobError("PDF page count exceeds its limit")
        syntax = pdf.check_pdf_syntax()
        if len(syntax) > 1000:
            raise JobError("PDF has too many syntax warnings")
        report = {
            "engine": "pikepdf",
            "version": pikepdf.__version__,
            "qpdf": pikepdf.__libqpdf_version__,
            "pages": len(pdf.pages),
            "syntax_warnings": syntax,
            "status": "partial" if syntax else "completed",
        }
        if operation == "optimize":
            pikepdf.settings.set_flate_compression_level(options["compression_level"])
            out = workdir / "optimized.pdf"
            pdf.save(
                out,
                compress_streams=True,
                recompress_flate=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                linearize=options["linearize"],
            )
            with pikepdf.open(out) as saved:
                post = saved.check_pdf_syntax()
                if len(saved.pages) != len(pdf.pages) or post:
                    raise JobError("Rewritten PDF failed page-count or syntax verification")
            add(out)
            report.update(
                before_bytes=path.stat().st_size,
                after_bytes=out.stat().st_size,
                compression_level=options["compression_level"],
                linearized=options["linearize"],
            )
            warnings.append(
                "Lossless stream rewriting does not validate semantics or "
                "signatures. Any prior signatures are invalidated."
            )
            if out.stat().st_size >= path.stat().st_size:
                warnings.append("The optimized PDF is not smaller than the original.")
        else:
            attachments = []
            total = 0
            for number, name in enumerate(pdf.attachments, 1):
                if number > min(limits["max_files"], 200):
                    raise JobError("Too many embedded attachments")
                entry = {"name": str(name), "kind": "embedded-file"}
                if operation == "attachments":
                    content = pdf.attachments[name].get_file().read_bytes()
                    total += len(content)
                    if (
                        len(content) > limits["max_file_bytes"]
                        or total > limits["max_output_bytes"]
                    ):
                        raise JobError("Embedded attachment exceeds output limits")
                    out = workdir / f"attachment-{number:04d}.bin"
                    out.write_bytes(content)
                    entry.update(
                        file=out.name, size=len(content), sha256=hashlib.sha256(content).hexdigest()
                    )
                    # Generated .bin names prevent executable/HTML extension surprises.
                    add(out, out.name)
                attachments.append(entry)
            report["attachments"] = attachments
            report["media_annotations"] = []
            report["has_outlines"] = "/Outlines" in pdf.Root
            report["has_acroform"] = "/AcroForm" in pdf.Root
            report["metadata"] = {str(k): str(v)[:4096] for k, v in list(pdf.docinfo.items())[:100]}
            for index, page in enumerate(pdf.pages):
                for annotation in page.obj.get("/Annots", []):
                    subtype = str(annotation.get("/Subtype", ""))
                    if subtype in {"/Sound", "/Movie", "/Screen", "/RichMedia", "/FileAttachment"}:
                        report["media_annotations"].append({"page": index + 1, "subtype": subtype})
                        if len(report["media_annotations"]) > 1000:
                            raise JobError("Too many media annotations")
            if operation == "attachments" and not attachments:
                report["status"] = "partial"
                warnings.append(
                    "No document-level embedded attachments found. Annotation "
                    "media streams are inventoried but not extracted."
                )
        if syntax:
            warnings.append(
                "qpdf reported syntax issues; inspect the report and original "
                "before using this output."
            )
        _json(workdir / "structure.json", report)
        add(workdir / "structure.json")
    return warnings, report["status"] == "partial"


def _raster(page, dpi):
    import pymupdf

    scale = min(dpi / 72, 6000 / max(page.rect.width, page.rect.height))
    if page.rect.width * page.rect.height * scale**2 > 25_000_000:
        raise JobError("Page raster exceeds the 25-million-pixel limit")
    return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)


def _deskew(image, options):
    from PIL import Image, ImageOps

    if options["mode"] == "manual":
        angle, confidence = options["angle"], None
    else:
        sample = ImageOps.grayscale(image)
        sample.thumbnail((640, 640))
        binary = sample.point(lambda pixel: 1 if pixel < 180 else 0)

        def score(angle):
            rotated = binary.rotate(
                angle, resample=Image.Resampling.NEAREST, expand=True, fillcolor=0
            )
            pixels, width = rotated.tobytes(), rotated.width
            rows = [sum(pixels[i : i + width]) for i in range(0, len(pixels), width)]
            return sum((b - a) ** 2 for a, b in pairwise(rows))

        coarse = [(score(i / 2), i / 2) for i in range(-20, 21)]
        best, angle = max(coarse, key=lambda item: (item[0], -abs(item[1])))
        fine = [
            (score(round(angle + i / 10, 1)), round(angle + i / 10, 1))
            for i in range(-4, 5)
            if -10 <= angle + i / 10 <= 10
        ]
        best, angle = max(fine, key=lambda item: (item[0], -abs(item[1])))
        baseline = score(0)
        confidence = best / max(baseline, 1)
        if best == 0 or abs(angle) >= 9.5 or (abs(angle) > 0.1 and confidence < 1.1):
            return image, {
                "angle": 0,
                "status": "partial",
                "reason": "No reliable small planar skew found",
                "score_ratio": confidence,
            }
    result = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
    return result, {"angle": angle, "status": "completed", "score_ratio": confidence}


def _scans(doc, options, workdir, operation, add, command):
    import pymupdf

    if not options["allow_rasterize"]:
        raise JobError("Explicitly acknowledge rasterization and loss of interactive PDF content")
    selected = _selected(options, len(doc), 50)
    if operation == "ocr_tesseract" and not re.fullmatch(
        r"[A-Za-z0-9_]{1,24}(\+[A-Za-z0-9_]{1,24}){0,3}", options["language"]
    ):
        raise JobError("Use installed language codes joined with +")
    reports, texts = [], []
    with pymupdf.open() as output:
        for number, index in enumerate(selected, 1):
            page, prefix = doc[index], workdir / f"page-{number:04d}"
            png = prefix.with_suffix(".png")
            pix = _raster(page, options["dpi"])
            if operation == "ocr_tesseract":
                pix.save(png)
                effective_dpi = round(72 * pix.width / page.rect.width)
                command(
                    [
                        "tesseract",
                        str(png),
                        str(prefix),
                        "-l",
                        options["language"],
                        "--dpi",
                        str(effective_dpi),
                        "--psm",
                        str(options["psm"]),
                        "pdf",
                        "txt",
                        "tsv",
                    ],
                    110,
                )
                with pymupdf.open(prefix.with_suffix(".pdf")) as recognized:
                    if len(recognized) != 1:
                        raise JobError("OCR did not produce exactly one page")
                    output.insert_pdf(recognized)
                text = prefix.with_suffix(".txt").read_text(encoding="utf-8")
                texts.append(f"--- Source page {index + 1} ---\n{text}")
                reports.append(
                    {
                        "page": index + 1,
                        "status": "completed" if text.strip() else "partial",
                        "characters": len(text),
                        "effective_dpi": effective_dpi,
                    }
                )
                add(prefix.with_suffix(".tsv"), f"source-page-{index + 1:04d}.tsv")
                prefix.with_suffix(".pdf").unlink()
                prefix.with_suffix(".txt").unlink()
                png.unlink()
            else:
                from PIL import Image

                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                corrected, report = _deskew(image, options)
                stream = io.BytesIO()
                corrected.save(stream, format="PNG")
                target = output.new_page(
                    width=corrected.width * page.rect.width / pix.width,
                    height=corrected.height * page.rect.height / pix.height,
                )
                target.insert_image(target.rect, stream=stream.getvalue())
                reports.append({"page": index + 1, **report})
        out = workdir / ("searchable.pdf" if operation == "ocr_tesseract" else "deskewed.pdf")
        output.save(out, garbage=4, deflate=True)
        add(out)
    if texts:
        (workdir / "recognized.txt").write_text("\n\n".join(texts), encoding="utf-8")
        add(workdir / "recognized.txt")
    partial = any(p["status"] == "partial" for p in reports)
    _json(
        workdir / "scan-report.json",
        {"operation": operation, "pages": reports, "status": "partial" if partial else "completed"},
    )
    add(workdir / "scan-report.json")
    return [
        (
            "Output contains the selected pages as images. Interactive content and "
            "prior signatures are not preserved. Review every page against the "
            "original."
        )
    ], partial


def process(request):
    import pymupdf

    from .catalog import validate_options
    from .worker import _command, _pdf, _validate_pdf

    operation = request["operation"]
    options = validate_options(operation, request["options"])
    workdir = Path(request["workdir"])
    outputs, warnings, partial = [], [], False
    stem = Path(safe_name(request["names"][0])).stem if request["names"] else "document"

    def add(path, name=None):
        if not path.is_file():
            raise JobError("Engine did not produce the required output")
        outputs.append({"file": path.name, "name": safe_name(name or f"{stem}-{path.name}")})
        return outputs[-1]["name"]

    if operation == "oxide":
        from .oxide import extract

        warnings, partial = extract(request, options, add)
    elif operation.startswith("oxide_"):
        from .oxide_routes import process as oxide_process

        warnings, partial = oxide_process(request, options, add)
    elif operation == "create":
        if request["inputs"]:
            raise JobError("Create expects no input files")
        out = workdir / "created.pdf"
        warnings = _create(options, out)
        _validate_pdf(out)
        add(out)
    else:
        path = Path(request["inputs"][0]).resolve(strict=True)
        with _pdf(path, request["limits"]["max_pages"], options.get("password", "")) as doc:
            if operation == "svg":
                from .svg import export_pages

                warnings = export_pages(doc, options, workdir, request["limits"], add)
            elif operation == "recolor":
                from .recolor import convert_document

                warnings = convert_document(doc, options, workdir, request["limits"], add)
            elif operation in {"tables", "geometry"}:
                warnings, partial = _plumber(path, doc, options, workdir, operation, add)
            elif operation in {"optimize", "inspectpdf", "attachments"}:
                warnings, partial = _pike(path, options, workdir, operation, request["limits"], add)
            elif operation == "paginate":
                out = workdir / "paginated.pdf"
                warnings = _paginate(doc, options, out)
                _validate_pdf(out, len(doc))
                add(out)
            elif operation == "deduplicate":
                out = workdir / "deduplicated.pdf"
                before = doc.xref_length()
                doc.save(out, garbage=4, deflate=True)
                _validate_pdf(out, len(doc))
                with pymupdf.open(out) as saved:
                    after = saved.xref_length()
                add(out)
                _json(
                    workdir / "deduplication.json",
                    {
                        "objects_before": before,
                        "objects_after": after,
                        "pages_preserved": len(doc),
                        "before_bytes": path.stat().st_size,
                        "after_bytes": out.stat().st_size,
                    },
                )
                add(workdir / "deduplication.json")
                warnings = [
                    (
                        "Only identical PDF objects and streams are deduplicated. "
                        "Pages and text occurrences remain. Prior signatures are "
                        "invalidated."
                    )
                ]
            elif operation in {"ocr_tesseract", "deskew"}:
                warnings, partial = _scans(doc, options, workdir, operation, add, _command)
                _validate_pdf(
                    workdir
                    / ("searchable.pdf" if operation == "ocr_tesseract" else "deskewed.pdf"),
                    len(_selected(options, len(doc), 50)),
                )
            elif operation == "render_native":
                for number, index in enumerate(_selected(options, len(doc)), 1):
                    out = workdir / f"page-{number:04d}.png"
                    if options["renderer"] == "pymupdf":
                        _raster(doc[index], options["dpi"]).save(out)
                    else:
                        single = workdir / "selected.pdf"
                        with pymupdf.open() as fragment:
                            fragment.insert_pdf(doc, from_page=index, to_page=index)
                            fragment.save(single)
                        page = doc[index]
                        dpi = min(
                            options["dpi"], 6000 * 72 / max(page.rect.width, page.rect.height)
                        )
                        if page.rect.width * page.rect.height * (dpi / 72) ** 2 > 25_000_000:
                            raise JobError("Page raster exceeds the 25-million-pixel limit")
                        _command(
                            [
                                "gs",
                                "-dSAFER",
                                "-dBATCH",
                                "-dNOPAUSE",
                                "-sDEVICE=png16m",
                                f"-r{dpi}",
                                f"-sOutputFile={out}",
                                "-f",
                                str(single),
                            ],
                            110,
                        )
                        single.unlink()
                    # Parse the PNG before publication, not merely the exit status.
                    pix = pymupdf.Pixmap(out)
                    if pix.width > 6001 or pix.height > 6001:
                        raise JobError("Renderer exceeded its pixel limit")
                    add(out, f"{stem}-page-{index + 1:04d}.png")
            else:
                raise JobError("Unsupported engine adapter")
    return {"ok": True, "outputs": outputs, "warnings": warnings, "partial": partial}
