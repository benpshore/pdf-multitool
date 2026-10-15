"""Disposable document worker. Invoke via the supervisor, not as a public server."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .core import JobError, parse_pages, safe_name


def _limits(config: dict, operation: str) -> None:
    if os.name != "posix":
        return  # Parent still provides time/output/cancellation bounds.
    import resource

    mb = config["docling_memory_mb"] if operation == "docling" else config["memory_mb"]
    seconds = (
        config["docling_timeout_seconds"] if operation == "docling" else config["timeout_seconds"]
    )
    for kind, value in [
        (resource.RLIMIT_AS, mb * 1024 * 1024),
        (resource.RLIMIT_CPU, seconds),
        (resource.RLIMIT_FSIZE, config["max_output_bytes"]),
        (resource.RLIMIT_NOFILE, 512),
        (resource.RLIMIT_CORE, 0),
    ]:
        _, hard = resource.getrlimit(kind)
        cap = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(kind, (cap, cap))


def _deny_network(event: str, args: tuple) -> None:
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise PermissionError("Network access is disabled inside document workers")


def _pdf(path: Path, max_pages: int, password: str = ""):
    import pymupdf

    doc = pymupdf.open(path)
    if not doc.is_pdf:
        doc.close()
        raise JobError("This operation requires a PDF")
    if doc.needs_pass and not doc.authenticate(password):
        doc.close()
        raise JobError("Encrypted PDF: the supplied password is missing or incorrect")
    if not 1 <= len(doc) <= max_pages:
        doc.close()
        raise JobError(f"PDF must have 1-{max_pages} pages")
    return doc


def _validate_pdf(path: Path, expected: int | None = None, password: str = "") -> None:
    with _pdf(path, 10000, password) as doc:
        if expected is not None and len(doc) != expected:
            raise JobError(f"Output page count {len(doc)} differs from expected {expected}")
        # Force page dictionaries to be parsed; a process exit alone isn't success.
        for page in doc:
            if page.rect.is_empty:
                raise JobError("Output contains an empty page rectangle")


def _command(args: list[str], timeout: int) -> None:
    executable = shutil.which(args[0])
    if not executable:
        raise JobError(f"Required engine {args[0]} is not installed")
    proc = subprocess.run(  # noqa: S603 -- fixed engine/CLI argv; no shell
        [executable, *args[1:]], stdin=subprocess.DEVNULL, timeout=timeout, check=False
    )
    if proc.returncode:
        raise JobError(f"{args[0]} exited with code {proc.returncode}; input may be malformed")


def _page_link_matrix(page):
    """Actual PDF-to-link coordinates, including rotated crop offsets.

    The pinned engine's public transformation_matrix drops those offsets for
    rotated pages. Its native transform is already rotated; do not rotate twice.
    """
    import pymupdf

    matrix = pymupdf.mupdf.FzMatrix()
    box = pymupdf.mupdf.FzRect()
    pymupdf.mupdf.pdf_page_transform(page._pdf_page(), box, matrix)
    return pymupdf.Matrix(pymupdf.JM_py_from_matrix(matrix))


def _insert_selected_pdf(destination, source, selected: list[int]) -> list[str]:
    """Copy unique selected pages and remap links between retained pages."""
    import pymupdf

    offset = len(destination)
    page_map = {page: offset + index for index, page in enumerate(selected)}
    contiguous = selected == list(range(selected[0], selected[-1] + 1))
    # One-page insertions silently lose widgets from radio groups spanning pages.
    # Bulk insertion retains complete groups. Partial/noncontiguous groups cannot
    # currently be copied safely; never flatten fields without explicit consent.
    radio_pages = {}
    named_pages = {}
    widget_counts = {}
    for page in source:
        widgets = list(page.widgets() or ())
        widget_counts[page.number] = len(widgets)
        if sum(widget_counts.values()) > 10000:
            raise JobError("PDF form widget count exceeds the supported copy limit")
        for widget in widgets:
            named_pages.setdefault(widget.field_name, set()).add(page.number)
            if widget.field_type != pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
                continue
            kind, parent = source.xref_get_key(widget.xref, "Parent")
            group = parent if kind == "xref" else f"widget:{widget.xref}"
            radio_pages.setdefault(group, set()).add(page.number)
    selected_set = set(selected)
    for group_pages in radio_pages.values():
        if (len(group_pages) > 1 and group_pages & selected_set) and (
            not contiguous or not group_pages <= selected_set
        ):
            raise JobError(
                "A radio-button group spans multiple pages and cannot be safely "
                "copied with this page selection. Select one ascending consecutive"
                " range containing every page of the group, or explicitly flatten "
                "the form in a separate operation first. No output was kept."
            )
    if not contiguous and any(len(pages & selected_set) > 1 for pages in named_pages.values()):
        raise JobError(
            "A form field occurs on multiple selected pages and cannot be "
            "safely copied in this order. Select one ascending consecutive "
            "range, or explicitly flatten the form in a separate operation "
            "first. No output was kept."
        )
    if contiguous:
        destination.insert_pdf(
            source,
            from_page=selected[0],
            to_page=selected[-1],
            links=False,
            annots=True,
            widgets=True,
        )
    else:
        for index in selected:
            destination.insert_pdf(
                source, from_page=index, to_page=index, links=False, annots=True, widgets=True
            )
    renamed = 0
    for index in selected:
        before = list(source[index].widgets() or ())
        after = list(destination[page_map[index]].widgets() or ())
        if len(after) != widget_counts[index]:
            raise JobError(
                "The PDF engine could not preserve every form widget while copying"
                " pages. No output was kept. Explicitly flatten the form in a "
                "separate operation before copying if editable fields are not "
                "needed."
            )
        renamed += sum(
            old.field_name != new.field_name for old, new in zip(before, after, strict=True)
        )
    omitted, unsupported = 0, 0
    # Inspect the actual action as well as MuPDF's lossy get_links view: named
    # actions may appear as GOTO and named GoToR may appear as launch filenames.
    for index in selected:
        page = source[index]
        raw_xrefs = {row[0] for row in page.annot_xrefs() if row[1] == pymupdf.PDF_ANNOT_LINK}
        unhandled = set(raw_xrefs)
        for link in page.get_links():
            xref = link.get("xref", 0)
            if xref not in raw_xrefs:
                # Unknown actions make get_links omit xrefs even for nearby
                # valid links. Resolve only a unique, matching raw rectangle.
                candidates = []
                for candidate in unhandled:
                    kind, value = source.xref_get_key(candidate, "Rect")
                    if kind != "array":
                        continue
                    try:
                        numbers = [float(v) for v in value.strip("[]").split()]
                        if len(numbers) != 4:
                            continue
                        rect = pymupdf.Rect(numbers).normalize() * _page_link_matrix(page)
                        if all(abs(a - b) < 0.001 for a, b in zip(rect, link["from"], strict=True)):
                            candidates.append(candidate)
                    except ValueError, TypeError:
                        continue
                if len(candidates) != 1:
                    continue
                xref = candidates[0]
            if xref not in unhandled:
                continue
            action = source.xref_get_key(xref, "A/S")[1]
            if (
                source.xref_get_key(xref, "AA")[0] != "null"
                or source.xref_get_key(xref, "A/Next")[0] != "null"
                or action not in {"null", "/GoTo", "/GoToR", "/URI", "/Launch"}
            ):
                continue
            copied = dict(link)
            copied.pop("xref", None)
            copied.pop("id", None)
            copied["from"] = (
                link["from"]
                * ~_page_link_matrix(page)
                * destination[page_map[index]].transformation_matrix
            )
            remote_kind, remote_dest = source.xref_get_key(xref, "A/D")
            if action == "/GoToR" and remote_kind in {"string", "name"}:
                file_kind, filename = source.xref_get_key(xref, "A/F")
                if file_kind != "string":
                    continue  # Complex file specifications need a separate copier.
                target_page = destination[page_map[index]]
                target_page.insert_link(
                    {"kind": pymupdf.LINK_URI, "from": copied["from"], "uri": "about:blank"}
                )
                new_xref = target_page.annot_xrefs()[-1][0]
                destination.xref_set_key(new_xref, "A", "<< /S /GoToR >>")
                destination.xref_set_key(new_xref, "A/F", pymupdf.get_pdf_str(filename))
                name_token = "/" + "".join(
                    f"#{byte:02X}" for byte in remote_dest[1:].encode("utf-8")
                )
                destination.xref_set_key(
                    new_xref,
                    "A/D",
                    pymupdf.get_pdf_str(remote_dest) if remote_kind == "string" else name_token,
                )
            else:
                if link["kind"] not in {
                    pymupdf.LINK_GOTO,
                    pymupdf.LINK_URI,
                    pymupdf.LINK_GOTOR,
                    pymupdf.LINK_LAUNCH,
                }:
                    continue
                if link["kind"] == pymupdf.LINK_GOTO:
                    target = link.get("page")
                    if target not in page_map:
                        unhandled.remove(xref)
                        omitted += 1
                        continue
                    copied["to"] = (
                        link["to"]
                        * ~_page_link_matrix(source[target])
                        * destination[page_map[target]].transformation_matrix
                    )
                    copied["page"] = page_map[target]
                destination[page_map[index]].insert_link(copied)
            unhandled.remove(xref)
        unsupported += len(unhandled)
    warnings = []
    if renamed:
        warnings.append(
            f"The PDF engine renamed {renamed}"
            f" form field widget(s) to avoid duplicate names. Review field names and "
            f"values before filling or automation."
        )
    if omitted:
        warnings.append(
            f"{omitted} internal link(s) were not copied because their target pages were omitted"
            f" from this output."
        )
    if unsupported:
        warnings.append(
            f"{unsupported}"
            f" unsupported or ambiguous link action(s) were not copied. Named actions, "
            f"chained/additional actions and complex file specifications may require "
            f"the original PDF. Originals are unchanged."
        )
    return warnings


def process(request: dict) -> dict:
    operation, options = request["operation"], request["options"]
    paths = [Path(p).resolve(strict=True) for p in request["inputs"]]
    names = request["names"]
    workdir = Path(request["workdir"])
    max_pages = request["limits"]["max_pages"]
    outputs, warnings = [], []
    stem = Path(safe_name(names[0])).stem[:150]
    password = options.get("password", "")
    if not isinstance(password, str) or len(password.encode("utf-8")) > 127:
        raise JobError("Invalid document password")

    def add(path: Path, name: str | None = None) -> None:
        if (not path.is_file() or path.stat().st_size == 0) and (
            path.suffix != ".txt" or not path.is_file()
        ):
            # Empty text is valid evidence of a possible scanned page.
            raise JobError("Engine did not create a usable output")
        outputs.append(
            {"file": path.relative_to(workdir).as_posix(), "name": safe_name(name or path.name)}
        )

    def image_warnings(report: dict) -> list[str]:
        # Storage IDs are implementation details, not useful document names.
        result = []
        for warning in report.get("warnings", []):
            for path, name in zip(paths, names, strict=True):
                if warning.startswith(path.name + ": "):
                    warning = safe_name(name) + warning[len(path.name) :]
                    break
            result.append(warning)
        return result

    if operation == "unpack":
        from .archives import ArchiveLimits, extract_archive

        destination = workdir / "extracted"
        destination.mkdir(mode=0o700)
        archive_limits = ArchiveLimits(
            max_compressed_bytes=request["limits"]["max_file_bytes"],
            max_member_bytes=request["limits"]["max_file_bytes"],
            max_expanded_bytes=min(
                request["limits"]["max_output_bytes"],
                int(options.get("max_expanded_bytes", 500 * 1024 * 1024)),
            ),
            max_files=min(request["limits"]["max_files"], int(options.get("max_files", 200))),
        )
        report = extract_archive(paths[0], destination, names[0], archive_limits)
        for item in report["files"]:
            add(Path(item["path"]), item["name"])
            outputs[-1]["member_path"] = item["member_path"]
        return {
            "ok": True,
            "outputs": outputs,
            "warnings": report["warnings"],
            "statistics": report["statistics"],
        }

    if operation == "bundle":
        from .archives import archive_output

        format = options.get("format", "zip")
        if format not in {"zip", "tar.gz"}:
            raise JobError("Archive format must be zip or tar.gz")
        out = workdir / ("results.zip" if format == "zip" else "results.tar.gz")
        items = [
            {"path": str(path), "name": name, "archive_path": arcpath}
            for path, name, arcpath in zip(
                paths, names, request.get("archive_paths", names), strict=True
            )
        ]
        archive_output(items, out, format=format)
        add(out)
        return {"ok": True, "outputs": outputs, "warnings": []}

    if operation == "html":
        if len(paths) != 1 or paths[0].stat().st_size > 5 * 1024 * 1024:
            raise JobError("HTML input must be at most 5 MB")
        from weasyprint import HTML

        from .html_resources import DenyAllFetcher

        fetcher = DenyAllFetcher()

        source = paths[0].read_text(encoding="utf-8-sig")
        out = workdir / "document.pdf"
        HTML(string=source, base_url=None, url_fetcher=fetcher).write_pdf(out)
        _validate_pdf(out)
        add(out, f"{stem}.pdf")
        if fetcher.blocked:
            warnings.append(
                "HTML resource loading was blocked. Use self-contained text and "
                "inline CSS; images, fonts and imports are not fetched."
            )
        return {"ok": True, "outputs": outputs, "warnings": warnings}

    import pymupdf

    from .images import IMAGE_SUFFIXES

    image_positions = [i for i, path in enumerate(paths) if path.suffix.lower() in IMAGE_SUFFIXES]
    if operation == "imagepdf":
        if len(paths) > max_pages:
            raise JobError(f"Combined PDF exceeds {max_pages} pages")
        from .images import images_to_pdf

        out = workdir / "images.pdf"
        report = images_to_pdf(paths, out, dpi=options.get("dpi", 144))
        _validate_pdf(out, len(paths))
        add(out, f"{stem}.pdf" if len(paths) == 1 else "images.pdf")
        return {"ok": True, "outputs": outputs, "warnings": image_warnings(report)}
    if image_positions and operation != "merge":
        from .images import images_to_pdf, inspect_image

        if operation == "identify":
            report = inspect_image(paths[0])
            report.update(
                name=safe_name(names[0]),
                input_type="image",
                suggested_filename=safe_name(names[0]),
                native_text_characters=0,
                ocr_candidate=True,
                ocr_performed=False,
                ocr_note=(
                    "An image has no native PDF text layer. OCR may be useful if it "
                    "contains text; content has not been recognized."
                ),
            )
            out = workdir / "analysis.json"
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            add(out, f"{stem}-analysis.json")
            return {"ok": True, "outputs": outputs, "warnings": report.get("warnings", [])}
        if operation == "preview":
            temporary = workdir / "preview-source.pdf"
            report = images_to_pdf(paths, temporary)
            with _pdf(temporary, max_pages) as image_document:
                page = image_document[0]
                scale = min(240 / page.rect.width, 300 / page.rect.height)
                out = workdir / "preview.png"
                page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(out)
            temporary.unlink()
            add(out)
            return {"ok": True, "outputs": outputs, "warnings": image_warnings(report)}
        raise JobError(
            "Image inputs support Images to PDF, Merge, Analyze and Preview. "
            "Convert to PDF first for other PDF operations. OCR is not "
            "performed by image conversion."
        )
    if operation == "identify" and not password:
        with pymupdf.open(paths[0]) as locked:
            if locked.is_pdf and locked.needs_pass:
                out = workdir / "analysis.json"
                out.write_text(
                    json.dumps(
                        {
                            "input_type": "PDF",
                            "input_protected": True,
                            "authentication": "password_required",
                            "suggested_filename": safe_name(names[0]),
                            "pages": None,
                            "warnings": [
                                (
                                    "Protected PDF detected. Supply its password "
                                    "to inspect content, "
                                    "forms and headings."
                                )
                            ],
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                add(out, f"{stem}-analysis.json")
                return {"ok": True, "outputs": outputs, "warnings": []}
    if operation == "merge":
        image_pdf = None
        if image_positions:
            from .images import images_to_pdf

            image_pdf = workdir / "normalized-images.pdf"
            report = images_to_pdf(
                [paths[i] for i in image_positions], image_pdf, dpi=options.get("dpi", 144)
            )
            warnings.extend(image_warnings(report))
        merged = pymupdf.open()
        count = 0
        protected_input = False
        for position, path in enumerate(paths):
            is_image = position in image_positions
            with _pdf(
                image_pdf if is_image else path, max_pages, "" if is_image else password
            ) as source:
                protected_input |= bool(source.metadata.get("encryption"))
                selected = parse_pages(
                    str(options.get("pages", "")), 1 if is_image else len(source)
                )
                count += len(selected)
                if count > max_pages:
                    raise JobError(f"Combined PDF exceeds {max_pages} pages")
                source_pages = [image_positions.index(position)] if is_image else selected
                warnings.extend(_insert_selected_pdf(merged, source, source_pages))
        out = workdir / "merged.pdf"
        merged.save(out, garbage=3, deflate=True)
        merged.close()
        if image_pdf is not None:
            image_pdf.unlink()
        _validate_pdf(out, count)
        add(out)
        warnings.append(
            "Merging preserves page content; signatures and document-level "
            "structure such as outlines/attachments are not guaranteed to "
            "survive."
        )
        if protected_input:
            warnings.append(
                "Merging protected inputs creates an unencrypted working copy. "
                "Encrypted originals are preserved; use Protect PDF to encrypt the"
                " merged output."
            )
        return {"ok": True, "outputs": outputs, "warnings": warnings}

    with _pdf(paths[0], max_pages, password) as doc:
        count = len(doc)
        pages_spec = str(options.get("pages", ""))
        # This MuPDF version's needs_pass query can reset an authenticated key.
        # Read the already-populated encryption metadata after authentication.
        input_protected = bool(doc.metadata.get("encryption"))
        if input_protected and operation == "rename":
            warnings.append(
                "The renamed PDF preserves its original encryption state. Its "
                "separate analysis JSON is unencrypted."
            )
        elif input_protected and operation != "protect":
            warnings.append(
                "Outputs from password-protected inputs are unencrypted working "
                "copies. The encrypted original is preserved; use Protect PDF to "
                "encrypt an output."
            )
        if operation in {"markdown", "textlayout"}:
            from .analysis import export_markdown, export_text

            out = workdir / ("document.md" if operation == "markdown" else "document.txt")
            export_options = {"skip_repeated_margins": options.get("skip_repeated_margins", False)}
            if operation == "markdown":
                export_options["include_images"] = options.get("include_images", True)
            report = (export_markdown if operation == "markdown" else export_text)(
                doc, out, names[0], export_options
            )
            for item in report["outputs"]:
                add(
                    Path(item["path"]),
                    f"{stem}{out.suffix}" if Path(item["path"]) == out else item["name"],
                )
            warnings.extend(report.get("warnings", []))
            if len(report["outputs"]) > 1:
                warnings.append(
                    "Markdown images are companion files. Package these results as ZIP"
                    " or TAR.GZ to preserve their relative links."
                )
        elif operation in {"identify", "pdfhtml", "rename", "repaginate"}:
            from .analysis import analyze_document, export_html
            from .document_ops import fields_report

            analysis = analyze_document(doc, names[0])
            analysis["forms_and_signatures"] = fields_report(doc)
            analysis["input_protected"] = input_protected
            suggested_stem = Path(safe_name(analysis.get("suggested_filename", names[0]))).stem
            report = workdir / "analysis.json"
            report.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
            if operation == "rename":
                out = workdir / "renamed.pdf"
                shutil.copyfile(paths[0], out)
                add(out, analysis.get("suggested_filename", names[0]))
                add(report, f"{suggested_stem}-analysis.json")
            elif operation == "identify":
                add(report, f"{suggested_stem}-analysis.json")
            else:
                out = workdir / "document.html"
                export_report = export_html(doc, out, names[0])
                warnings.extend(export_report.get("warnings", []))
                if operation == "pdfhtml":
                    add(out, f"{suggested_stem}.html")
                    add(report, f"{suggested_stem}-analysis.json")
                else:
                    from weasyprint import HTML

                    from .html_resources import GeneratedImageFetcher

                    pdf_out = workdir / "repaginated.pdf"
                    HTML(
                        string=out.read_text(encoding="utf-8"), url_fetcher=GeneratedImageFetcher()
                    ).write_pdf(pdf_out)
                    _validate_pdf(pdf_out)
                    add(pdf_out, f"{suggested_stem}-repaginated.pdf")
                    add(out, f"{suggested_stem}-reflow.html")
                    warnings.append(
                        "Repagination is reconstructed from extracted content. Original "
                        "layout, formulas and complex tables may differ."
                    )
        elif operation == "signatures":
            from .document_ops import fields_report

            out = workdir / "signatures.json"
            out.write_text(
                json.dumps(fields_report(doc), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            add(out, f"{stem}-signatures.json")
            warnings.append(
                "This is signature-field inspection only. Cryptographic "
                "verification and digital signing are not implemented."
            )
        elif operation in {"clean", "forms", "redact", "unlock", "protect"}:
            from .document_ops import transform

            inspect_forms = (
                operation == "forms" and "fields" not in options and "create_fields" not in options
            )
            out = workdir / ("forms.json" if inspect_forms else f"{operation}.pdf")
            report = transform(doc, operation, out, options)
            warnings.extend(report.get("warnings", []))
            if not inspect_forms:
                _validate_pdf(
                    out, count, options.get("new_password", "") if operation == "protect" else ""
                )
            add(out, f"{stem}-{operation}{out.suffix}")
            if report.get("detail"):
                detail_out = workdir / "changes.json"
                detail_out.write_text(json.dumps(report["detail"], indent=2), encoding="utf-8")
                add(detail_out, f"{stem}-{operation}-changes.json")
        elif operation == "split":
            groups = (
                [[i] for i in range(count)]
                if not pages_spec.strip()
                else [parse_pages(g, count) for g in pages_spec.split(";")]
            )
            if len(groups) > 500:
                raise JobError(
                    "Split is limited to 500 output files per input; select larger page groups"
                )
            for number, group in enumerate(groups, 1):
                out = workdir / f"part-{number:04d}.pdf"
                with pymupdf.open() as fragment:
                    warnings.extend(_insert_selected_pdf(fragment, doc, group))
                    fragment.save(out, garbage=3, deflate=True)
                _validate_pdf(out, len(group))
                add(out, f"{stem}-{number:04d}.pdf")
            warnings.append(
                "Split outputs may not retain document-level outlines, attachments or signatures."
            )
        elif operation == "rotate":
            rotation = int(options.get("rotation", 90))
            if rotation not in {90, 180, 270}:
                raise JobError("Rotation must be 90, 180 or 270 degrees clockwise")
            for index in parse_pages(pages_spec, count):
                doc[index].set_rotation((doc[index].rotation + rotation) % 360)
            out = workdir / "rotated.pdf"
            doc.save(out, garbage=3, deflate=True)
            _validate_pdf(out, count)
            add(out, f"{stem}-rotated.pdf")
            warnings.append("Changing a signed PDF invalidates its digital signatures.")
        elif operation == "preview":
            page = doc[0]
            scale = min(240 / page.rect.width, 300 / page.rect.height)
            out = workdir / "preview.png"
            page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(out)
            add(out)
        elif operation in {"text", "render"}:
            selected = parse_pages(pages_spec, count)
            selected_pdf = paths[0]
            if selected != list(range(count)) or password:
                selected_pdf = workdir / "selected.pdf"
                with pymupdf.open() as fragment:
                    for index in selected:
                        fragment.insert_pdf(doc, from_page=index, to_page=index)
                    fragment.save(selected_pdf)
            if operation == "text":
                out = workdir / "text.txt"
                _command(
                    ["pdftotext", "-layout", "-enc", "UTF-8", str(selected_pdf), str(out)], 110
                )
                add(out, f"{stem}.txt")
                text = out.read_text(encoding="utf-8")
                if not text.strip():
                    warnings.append(
                        "No embedded text was extracted. These pages may need OCR; an "
                        "empty result does not prove the document has no visible text."
                    )
                else:
                    warnings.append(
                        "Embedded text extraction can misorder columns or lose "
                        "mathematical/chemical formatting. Review against the original; no"
                        " character-completeness score is inferred."
                    )
            else:
                dpi = int(options.get("dpi", 120))
                if not 36 <= dpi <= 300:
                    raise JobError("DPI must be between 36 and 300")
                if len(selected) > 100:
                    raise JobError("Rendering is limited to 100 selected pages per input")
                for number, original_index in enumerate(selected, 1):
                    prefix = workdir / f"page-{number:04d}"
                    args = [
                        "pdftoppm",
                        "-png",
                        "-r",
                        str(dpi),
                        "-f",
                        str(number),
                        "-l",
                        str(number),
                        "-singlefile",
                    ]
                    # Poppler's scale-to also upscales. Use it only as an upper cap.
                    rect = doc[original_index].rect
                    if max(rect.width, rect.height) * dpi / 72 > 6000:
                        args.extend(["-scale-to", "6000"])
                    _command([*args, str(selected_pdf), str(prefix)], 110)
                for number, out in enumerate(sorted(workdir.glob("page-*.png")), 1):
                    add(out, f"{stem}-page-{selected[number - 1] + 1:04d}.png")
                if len(outputs) != len(selected):
                    raise JobError("Render output page count did not match the selected pages")
        elif operation == "compress":
            if options.get("allow_lossy") is not True:
                raise JobError(
                    "Compression rewrites the PDF and can lose nonvisual content; "
                    "explicitly acknowledge this warning first"
                )
            profile = str(options.get("compress_profile", "ebook"))
            if profile not in {"screen", "ebook", "printer", "prepress"}:
                raise JobError("Invalid compression profile")
            out = workdir / "compressed.pdf"
            source_path = paths[0]
            if password:
                source_path = workdir / "unlocked-input.pdf"
                doc.save(source_path, encryption=pymupdf.PDF_ENCRYPT_NONE)
            _command(
                [
                    "gs",
                    "-dSAFER",
                    "-dBATCH",
                    "-dNOPAUSE",
                    "-sDEVICE=pdfwrite",
                    "-dCompatibilityLevel=1.7",
                    f"-dPDFSETTINGS=/{profile}",
                    f"-sOutputFile={out}",
                    "-f",
                    str(source_path),
                ],
                110,
            )
            _validate_pdf(out, count)
            add(out, f"{stem}-compressed.pdf")
            warnings.append(
                "Ghostscript compression is a native rewrite. It may lose forms, "
                "tags, layers, attachments, links, signatures or other nonvisual "
                "content. Keep the original."
            )
            if out.stat().st_size >= paths[0].stat().st_size:
                warnings.append("The rewritten PDF is not smaller than the original.")
        elif operation == "docling":
            from .models import validate_model_manifest

            models = validate_model_manifest(os.environ.get("PDF_MULTITOOL_MODELS"))
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import EasyOcrOptions, PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption

            config = PdfPipelineOptions(
                artifacts_path=models, enable_remote_services=False, allow_external_plugins=False
            )
            config.do_ocr = options.get("ocr", True) is True
            config.ocr_options = EasyOcrOptions(
                download_enabled=False,
                use_gpu=False,
                model_storage_directory=str(models / "EasyOcr"),
            )
            config.do_table_structure = True
            converter = DocumentConverter(
                allowed_formats=[InputFormat.PDF],
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=config)},
            )
            result = converter.convert(
                paths[0],
                raises_on_error=True,
                max_num_pages=max_pages,
                max_file_size=request["limits"]["max_file_bytes"],
            )
            out = workdir / "document.json"
            out.write_text(
                json.dumps(result.document.export_to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            add(out, f"{stem}-structured.json")
            out = workdir / "document.md"
            out.write_text(result.document.export_to_markdown(), encoding="utf-8")
            add(out, f"{stem}.md")
            warnings.append(
                "OCR and inferred reading order require review, particularly "
                "formulas, citations and chemical notation."
            )
        else:
            raise JobError("Unsupported operation")
    return {"ok": True, "outputs": outputs, "warnings": warnings}


def main() -> None:
    os.umask(0o077)
    encoded = sys.stdin.buffer.read(256 * 1024 + 1)
    if len(encoded) > 256 * 1024:
        raise SystemExit("Job configuration exceeds its limit")
    request = json.loads(encoded)
    workdir = Path(request["workdir"])
    try:
        _limits(request["limits"], request["operation"])
        sys.addaudithook(_deny_network)
        result = process(request)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:2000]}
    (workdir / "result.json").write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
