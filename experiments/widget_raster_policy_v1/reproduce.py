"""Reproduce the two reviewed widget controls without changing the pixel oracle."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

import pikepdf
import pymupdf
import pypdfium2 as pdfium
from PIL import Image, ImageChops

from pdf_multitool import oxide_forms
from pdf_multitool.core import Job, Limits

ROOT = Path(__file__).resolve().parent
CASES = ("retained-3941", "precision-control-1238")
MODES = ("default", "lcd", "print", "no-smooth", "no-form-draw")
PAIRS = (("source", "native"), ("native", "manual"), ("native", "freetext"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def verify_manifest():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        path = ROOT / name
        require(path.is_file() and digest(path) == expected, f"Changed evidence: {name}")
    require(
        digest(Path(oxide_forms.__file__)) == manifest["source_bindings"]["oxide_forms.py"],
        "Imported forms source differs from the reviewed checkpoint",
    )
    require(
        digest(ROOT.parents[1] / "tests/test_oxide_forms.py")
        == manifest["source_bindings"]["test_oxide_forms.py"],
        "Forms tests differ from the reviewed checkpoint",
    )


def difference(a, b):
    require(a.size == b.size and a.mode == b.mode == "RGB", "Image geometry/mode differs")
    delta = ImageChops.difference(a, b)
    bounds = delta.getbbox()
    return {
        "changed_pixels": sum(pixel != (0, 0, 0) for pixel in delta.get_flattened_data()),
        "bounds": list(bounds) if bounds else None,
        "max_channel_delta": max(hi for _, hi in delta.getextrema()),
        "exact_pixel_parity": bounds is None,
    }


def render_pdfium(path, mode, scale, *, page_number=0, save_document=None):
    options = {"draw_annots": True, "may_draw_forms": mode != "no-form-draw"}
    if mode == "no-smooth":
        options["no_smoothtext"] = True
    if mode in {"lcd", "print"}:
        options["optimize_mode"] = mode
    with pdfium.PdfDocument(path) as pdf:
        pdf.init_forms(pdfium.raw.FPDF_FORMFILLINFO(version=2, xfa_disabled=True))
        page = pdf[page_number]
        bitmap = page.render(scale=scale, **options)
        image = bitmap.to_pil().convert("RGB").copy()
        bitmap.close()
        page.close()
        if save_document:
            pdf.save(save_document)
    return image


def graph(value):
    """Decode the complete acyclic fixture graph independently of the engine verifier."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float | Decimal):
        return {"number": str(Fraction(value))}
    if isinstance(value, pikepdf.Stream):
        return {
            "decoded_hex": value.read_bytes().hex(),
            "dictionary": {
                key: graph(item)
                for key, item in sorted(value.items())
                if key not in {"/Length", "/Filter", "/DecodeParms"}
            },
        }
    if isinstance(value, pikepdf.Dictionary):
        return {key: graph(item) for key, item in sorted(value.items())}
    if isinstance(value, pikepdf.Array):
        return [graph(item) for item in value]
    if isinstance(value, pikepdf.Name):
        return {"name": str(value)}
    if isinstance(value, pikepdf.String):
        return {"string_hex": bytes(value).hex()}
    raise TypeError(f"Unexpected fixture object type: {type(value).__name__}")


def appearance(pdf, role):
    page = pdf.pages[0]
    if role in {"source", "source-after-render", "freetext"}:
        return page.Annots[0].AP.N
    forms = [obj for obj in page.Resources.XObject.values() if obj.Subtype == pikepdf.Name.Form]
    require(len(forms) == 1, "Expected exactly one saved appearance")
    return forms[0]


def content(page):
    streams = page.Contents if isinstance(page.Contents, pikepdf.Array) else [page.Contents]
    return b"".join(stream.read_bytes() for stream in streams)


def verify_structure(paths):
    graphs = {}
    for role, path in paths.items():
        with pikepdf.open(path) as pdf:
            graphs[role] = graph(appearance(pdf, role))
    # Native output explicitly writes the optional FormType=1 default. Preserve
    # raw dictionaries in the evidence and normalize only that documented default.
    normalized = []
    for value in graphs.values():
        dictionary = {"/FormType": {"number": "1"}, **value["dictionary"]}
        require(dictionary["/FormType"] == {"number": "1"}, "Unexpected FormType")
        normalized.append({**value, "dictionary": dictionary})
    require(all(g == normalized[0] for g in normalized), "Appearance graph changed")
    with pikepdf.open(paths["source"]) as source, pikepdf.open(paths["native"]) as native:
        original, saved = source.pages[0], native.pages[0]
        require(content(saved).startswith(content(original)), "Original content prefix changed")
        for category, values in original.Resources.items():
            for name, value in values.items():
                require(graph(saved.Resources[category][name]) == graph(value), "Resource changed")
        for a, b in zip(source.pages, native.pages, strict=True):
            for key in ("/MediaBox", "/CropBox", "/Rotate", "/UserUnit"):
                require(graph(a.obj.get(key)) == graph(b.obj.get(key)), f"Page {key} changed")
        require(content(source.pages[1]) == content(native.pages[1]), "Control content changed")
        require(
            graph(source.pages[1].Resources) == graph(native.pages[1].Resources),
            "Control resources changed",
        )
        bbox = list(map(Fraction, original.Annots[0].AP.N.BBox))
        rect = list(map(Fraction, original.Annots[0].Rect))
        instructions = list(pikepdf.parse_content_stream(saved))
        require(str(instructions[-1].operator) == "Q", "Expected closed appearance draw")
        require(str(instructions[-2].operator) == "Do", "Expected appearance invocation")
        require(str(instructions[-3].operator) == "cm", "Expected placement matrix")
        a, b, c, d, e, f = map(Fraction, instructions[-3].operands)
        require(b == c == 0, "Unexpected placement shear/rotation")
        actual = [a * bbox[0] + e, d * bbox[1] + f, a * bbox[2] + e, d * bbox[3] + f]
        require(actual == rect, "Exact fixture appearance geometry changed")
    return graphs


def create_derivatives(source, target):
    paths = {"source": source}
    with TemporaryDirectory(prefix="widget-job-", dir=target) as temporary:
        job = Job(
            "oxide_form_flatten",
            [{"id": "fixture", "name": "source.pdf", "path": str(source)}],
            {"pages": "1", "allow_flatten": True},
            Path(temporary) / "jobs",
            Limits(),
        )
        before = digest(source)
        job.run()
        require(digest(source) == before, "Original fixture changed")
        require(job.state == "completed", f"Supervised flatten job failed: {job.errors}")
        outputs = [Path(item["path"]) for item in job.results if item["path"].endswith(".pdf")]
        require(len(outputs) == 1, "Expected one native PDF")
        paths["native"] = target / "native-output.pdf"
        shutil.copyfile(outputs[0], paths["native"])
    paths["manual"] = target / "manual-identical-appearance-on-page.pdf"
    with pikepdf.open(source) as pdf:
        page = pdf.pages[0]
        widget = page.Annots[0]
        page.Resources.XObject.ManualAppearance = widget.AP.N
        bbox, rect = list(map(float, widget.AP.N.BBox)), list(map(float, widget.Rect))
        sx = (rect[2] - rect[0]) / (bbox[2] - bbox[0])
        sy = (rect[3] - rect[1]) / (bbox[3] - bbox[1])
        tx, ty = rect[0] - sx * bbox[0], rect[1] - sy * bbox[1]
        overlay = f"q {sx:.12f} 0 0 {sy:.12f} {tx:.12f} {ty:.12f} cm /ManualAppearance Do Q\n"
        page.Contents = pikepdf.Array([page.Contents, pikepdf.Stream(pdf, overlay.encode())])
        del page.obj["/Annots"]
        del pdf.Root["/AcroForm"]
        pdf.save(paths["manual"])
    paths["freetext"] = target / "same-appearance-as-freetext.pdf"
    with pikepdf.open(source) as pdf:
        widget = pdf.pages[0].Annots[0]
        widget.Subtype = pikepdf.Name.FreeText
        for key in ("/FT", "/T", "/V"):
            del widget[key]
        widget.Contents = "Synthetic diagnostic: unchanged normal appearance"
        del pdf.Root["/AcroForm"]
        pdf.save(paths["freetext"])
    return paths


def diagnose(case, output):
    frozen = ROOT / "observed" / case
    expected = json.loads((frozen / "diagnosis.json").read_text())
    target = output / case
    target.mkdir()
    source = target / "source.pdf"
    shutil.copyfile(frozen / "source.pdf", source)
    paths = create_derivatives(source, target)
    comparisons = []
    for scale in (1, 2, 4):
        for mode in MODES:
            images = {}
            for role, path in paths.items():
                saved = (
                    target / "source-after-pdfium-render.pdf"
                    if role == "source" and scale == 2 and mode == "default"
                    else None
                )
                images[role] = render_pdfium(path, mode, scale, save_document=saved)
                images[role].save(target / f"{role}-{mode}-{scale}x.png")
                with Image.open(frozen / f"{role}-{mode}-{scale}x.png") as original:
                    require(
                        difference(images[role], original)["exact_pixel_parity"],
                        f"Render drift: {case}/{role}/{mode}/{scale}",
                    )
            for a, b in PAIRS:
                comparisons.append(
                    {
                        "mode": mode,
                        "scale": scale,
                        "a": a,
                        "b": b,
                        **difference(images[a], images[b]),
                    }
                )
    require(comparisons == expected["comparisons"], "Recorded PDFium comparison changed")
    paths["source-after-render"] = target / "source-after-pdfium-render.pdf"
    graphs = verify_structure(paths)
    write_json(target / "complete-appearance-graphs.json", graphs)
    mupdf, extracts = [], {}
    for scale in (1, 2, 4):
        images = {}
        for role in ("source", "native", "manual"):
            with pymupdf.open(paths[role]) as pdf:
                pix = pdf[0].get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale), annots=True, alpha=False
                )
                images[role] = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                images[role].save(target / f"{role}-mupdf-{scale}x.png")
                extracts[role] = {
                    "pages": [page.get_text() for page in pdf],
                    "widget_values": [w.field_value for page in pdf for w in page.widgets() or []],
                }
        for role in ("native", "manual"):
            delta = difference(images["source"], images[role])
            require(delta["exact_pixel_parity"], "MuPDF source/output mismatch")
            mupdf.append({"scale": scale, "a": "source", "b": role, **delta})
    require(
        extracts == json.loads((frozen / "independent-text.json").read_text()),
        "Source/output text changed",
    )
    for renderer in ("pdfium", "mupdf"):
        if renderer == "pdfium":
            a = render_pdfium(source, "default", 2, page_number=1)
            b = render_pdfium(paths["native"], "default", 2, page_number=1)
            require(difference(a, b)["exact_pixel_parity"], "PDFium control page changed")
        else:
            with pymupdf.open(source) as a, pymupdf.open(paths["native"]) as b:
                require(a[1].get_pixmap().samples == b[1].get_pixmap().samples, "Control changed")
    write_json(target / "independent-text.json", extracts)
    result = {
        "case": case,
        "pdfium": comparisons,
        "mupdf": mupdf,
        "appearance_graphs_equal_after_explicit_formtype_default": True,
    }
    write_json(target / "reproduction.json", result)
    print(json.dumps({"case": case, "pdfium_comparisons": 45, "mupdf_comparisons": 6}), flush=True)
    return result


def strict_oracle():
    """Keep the original exact-pixel assertion; a diagnostic match cannot satisfy it."""
    failures = []
    for case in CASES:
        frozen = ROOT / "observed" / case
        source = render_pdfium(frozen / "source.pdf", "default", 2)
        native = render_pdfium(frozen / "native-output.pdf", "default", 2)
        delta = difference(source, native)
        print(json.dumps({"case": case, **delta}), flush=True)
        if ImageChops.difference(source, native).getbbox() is not None:
            failures.append(case)
    if failures:
        raise AssertionError(f"Strict unchanged-default pixel parity failed: {', '.join(failures)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("diagnosis", "strict-oracle"), default="diagnosis")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    verify_manifest()
    if args.mode == "strict-oracle":
        strict_oracle()
        return
    if args.output is None:
        parser.error("diagnosis requires a new --output directory")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    results = [diagnose(case, args.output) for case in CASES]
    write_json(
        args.output / "summary.json",
        {"diagnosis_reproduced": True, "strict_pixel_parity": False, "cases": results},
    )


if __name__ == "__main__":
    main()
