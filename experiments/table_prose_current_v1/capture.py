"""Current-main adapter of the unchanged historical measurement harness.

Exit success means capture completed, not that semantic expectations passed.
Use --output /tmp/representation-run for independent reproduction.
"""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "experiments/representation_v1"
CURRENT_MAIN = "fb146a976ab1dce742a1960ef46f1165dfca731d"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    executable = shutil.which("git")
    if not executable:
        raise RuntimeError("git is required to identify the frozen checkpoint")
    return subprocess.check_output(  # noqa: S603 -- fixed git inspection, no shell
        [executable, *args], cwd=REPO, text=True
    ).strip()


def normalized(text):
    return " ".join(text.split())


def block_text(blocks):
    return normalized(
        " ".join(
            "".join(span.get("text", "") for span in line.get("spans", []))
            for block in blocks
            for line in block.get("lines", [])
        )
    )


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def portable(value, output):
    if isinstance(value, dict):
        return {k: portable(v, output) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [portable(v, output) for v in value]
    if isinstance(value, str) and value.startswith(str(output) + "/"):
        return str(Path(value).relative_to(output))
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError("Refusing to overwrite measurement evidence; use a fresh directory")
    subprocess.run(  # noqa: S603 -- fixed local verifier
        [sys.executable, str(ROOT / "build_fixtures.py"), "--check"], check=True
    )
    freeze_commit = "ac6a1d1fedbd60f5e34d6fe9f7a8ca01af46d211"
    if not freeze_commit:
        raise RuntimeError("Commit the freeze before measuring")
    frozen = json.loads((ROOT / "FROZEN.json").read_text())
    if git("diff", CURRENT_MAIN, "--", "src", "uv.lock", "pyproject.toml"):
        raise RuntimeError("Production or dependencies differ from pinned current main")
    original_tree = json.loads(Path(__file__).with_name("original_tree_sha256.json").read_text())
    actual_tree = {
        str(p.relative_to(ROOT)): sha(p.read_bytes())
        for p in sorted(ROOT.rglob("*"))
        if p.is_file()
    }
    if actual_tree != original_tree:
        raise RuntimeError("Original experiment tree changed")
    # Import the parser and application only after checking the committed freeze.
    import pymupdf

    from pdf_multitool import analysis

    expected_analysis = (REPO / "src/pdf_multitool/analysis.py").resolve()
    if Path(analysis.__file__).resolve() != expected_analysis:
        raise RuntimeError("Imported application is outside the pinned checkout")
    git_executable = shutil.which("git")
    expected_source = subprocess.check_output(  # noqa: S603 -- fixed source inspection
        [git_executable, "show", f"{CURRENT_MAIN}:src/pdf_multitool/analysis.py"], cwd=REPO
    )
    if expected_analysis.read_bytes() != expected_source:
        raise RuntimeError("Imported analysis bytes differ from pinned current main")

    expectations = json.loads((ROOT / "expectations.json").read_text())
    output.mkdir(parents=True, exist_ok=False)
    rows = {}
    for name, expected in expectations["fixtures"].items():
        source = ROOT / "fixtures" / f"{name}.pdf"
        before = source.read_bytes()
        case = output / name
        case.mkdir()
        with pymupdf.open(source) as doc:
            if len(doc) != 1 or doc.is_repaired:
                raise RuntimeError(f"Unexpected page count or repaired fixture: {name}")
            page = doc[0]
            renders = {}
            for dpi in (72, 144):
                pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False, annots=False)
                png = pix.tobytes("png")
                (case / f"render-{dpi}.png").write_bytes(png)
                renders[str(dpi)] = {
                    "width": pix.width,
                    "height": pix.height,
                    "channels": pix.n,
                    "samples_sha256": sha(pix.samples),
                    "png_sha256": sha(png),
                }
            flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
            parser_dict = page.get_text("dict", flags=flags, sort=False)
            raw_dict = page.get_text("rawdict", flags=flags, sort=False)
            write_json(
                case / "pymupdf.json",
                {
                    "dict": parser_dict,
                    "rawdict": raw_dict,
                    "text_unsorted": page.get_text("text", sort=False),
                    "text_sorted": page.get_text("text", sort=True),
                    "words_unsorted": page.get_text("words", sort=False),
                    "words_sorted": page.get_text("words", sort=True),
                    "texttrace": page.get_texttrace(),
                    "bboxlog": page.get_bboxlog(),
                    "xobjects": page.get_xobjects(),
                    "fonts": page.get_fonts(full=True),
                },
            )
            split = analysis._split_column_blocks(parser_dict["blocks"], page.rect.width)
            ordered = analysis._ordered_blocks(page, include_images=False)
            report = analysis.analyze_document(doc, source.name)
            reports = {
                "text": analysis.export_text(doc, case / "export.txt", source.name),
                "markdown": analysis.export_markdown(doc, case / "export.md", source.name),
                "html": analysis.export_html(doc, case / "export.html", source.name),
            }
            write_json(
                case / "application.json",
                portable(
                    {
                        "analysis": report,
                        "split_column_blocks": split,
                        "ordered_blocks": ordered,
                        "export_reports": reports,
                    },
                    output,
                ),
            )
            text = (case / "export.txt").read_text()
            prefix = "[Source page 1]\n\n"
            if not text.startswith(prefix):
                raise RuntimeError("Unexpected structured-text preamble; do not guess a new scorer")
            body = normalized(text[len(prefix) :])
            trace = page.get_texttrace()
            trace_text = "".join(chr(c[0]) for span in trace for c in span["chars"])
            rows[name] = {
                "pdf_sha256": sha(before),
                "renders": renders,
                "expected_text": expected,
                "trace_character_text": trace_text,
                "parser_block_text": block_text(parser_dict["blocks"]),
                "parser_blocks": len(parser_dict["blocks"]),
                "parser_lines": sum(len(b.get("lines", [])) for b in parser_dict["blocks"]),
                "parser_spans": report["text_span_count"],
                "split_block_text": block_text(split),
                "split_blocks": len(split),
                "application_ordered_text": block_text(ordered),
                "structured_text_body": body,
                "ordered_matches_expectation": block_text(ordered) == expected,
                "export_matches_expectation": body == expected,
            }
        if source.read_bytes() != before:
            raise RuntimeError("Source fixture changed during measurement")
    groups = {}
    for group, names in expectations["render_comparisons"].items():
        groups[group] = {
            "fixtures": names,
            "equal_render_samples": {
                str(dpi): len({rows[n]["renders"][str(dpi)]["samples_sha256"] for n in names}) == 1
                for dpi in (72, 144)
            },
            "equal_parser_text": len({rows[n]["parser_block_text"] for n in names}) == 1,
            "equal_application_order": len({rows[n]["application_ordered_text"] for n in names})
            == 1,
            "equal_structured_text": len({rows[n]["structured_text_body"] for n in names}) == 1,
        }
    summary = {
        "protocol": (
            "Observed expectation failures are retained, not assertions of product success."
        ),
        "freeze_commit": freeze_commit,
        "frozen_manifest_sha256": sha((ROOT / "FROZEN.json").read_bytes()),
        "production_base": CURRENT_MAIN,
        "historical_base": frozen["base_commit"],
        "original_experiment_commit": "d6dbcc6e4bbbef082ef80c475bdb2ce2fe4ba595",
        "original_harness_sha256": sha((ROOT / "measure.py").read_bytes()),
        "lock_sha256": sha((REPO / "uv.lock").read_bytes()),
        "actual_analysis_path": str(Path(analysis.__file__).resolve()),
        "adapter_note": (
            "Historical src guard explicitly replaced by pinned current-main guard; "
            "original harness unchanged."
        ),
        "analysis_py_sha256": sha(Path(analysis.__file__).read_bytes()),
        "measurement_script_sha256": sha(Path(__file__).read_bytes()),
        "runtime": {
            "python": platform.python_version(),
            "pymupdf": pymupdf.VersionBind,
            "mupdf": pymupdf.VersionFitz,
            "platform": platform.platform(),
        },
        "cases": rows,
        "groups": groups,
    }
    write_json(output / "summary.json", summary)
    for name, row in rows.items():
        print(
            name,
            "MATCH" if row["export_matches_expectation"] else "MISMATCH",
            repr(row["structured_text_body"]),
        )
    print(json.dumps(groups, indent=2))


if __name__ == "__main__":
    main()
