from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

from .catalog import READING_ORDER_OPERATIONS, READING_ORDERS
from .core import Job, JobError, Limits, engines, read_local_json, run_worker, validate_inputs


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="pdf-multitool", description="Private PDF workbench and batch CLI"
    )
    commands = root.add_subparsers(dest="command", required=True)
    gui = commands.add_parser("gui", help="Start a single-user loopback browser UI")
    gui.add_argument("--port", type=int, default=0)
    gui.add_argument("--no-browser", action="store_true")
    commands.add_parser("doctor", help="Show actual engine availability and version evidence")
    for operation in (
        "merge",
        "split",
        "rotate",
        "render",
        "text",
        "html",
        "compress",
        "docling",
        "identify",
        "pdfhtml",
        "rename",
        "repaginate",
        "clean",
        "forms",
        "signatures",
        "redact",
        "unlock",
        "protect",
        "unpack",
        "bundle",
        "markdown",
        "textlayout",
        "imagepdf",
    ):
        cmd = commands.add_parser(operation)
        cmd.add_argument("inputs", nargs="+", type=Path)
        cmd.add_argument(
            "-o",
            "--output",
            type=Path,
            default=Path("output"),
            help="Parent directory; every run gets a unique child directory",
        )
        if operation in {"merge", "split", "rotate", "render", "text", "redact"}:
            cmd.add_argument(
                "--pages", default="", help="1,3-5; for split use semicolon-separated output groups"
            )
        if operation not in {"html", "unpack", "bundle", "imagepdf"}:
            cmd.add_argument(
                "--ask-password",
                action="store_true",
                help="Read a document password privately from the terminal",
            )
        if operation == "bundle":
            cmd.add_argument("--format", choices=("zip", "tar.gz"), default="zip")
        if operation in READING_ORDER_OPERATIONS:
            cmd.add_argument(
                "--reading-order",
                choices=READING_ORDERS,
                default="auto",
                help=(
                    "Whole-document geometric order; explicit rows/columns require "
                    "unrotated horizontal LTR text and do not verify table semantics"
                ),
            )
        if operation in {"markdown", "textlayout"}:
            cmd.add_argument(
                "--skip-repeated-margins",
                action="store_true",
                help="Omit only strongly detected repeating margin lines; default preserves them",
            )
        if operation == "markdown":
            cmd.add_argument("--no-images", dest="include_images", action="store_false")
        if operation == "forms":
            form_mode = cmd.add_mutually_exclusive_group()
            form_mode.add_argument(
                "--fields-json",
                type=Path,
                help=(
                    "Local JSON object mapping field names to values, including radio "
                    "export values; omit to inspect"
                ),
            )
            form_mode.add_argument(
                "--create-fields-json",
                type=Path,
                help="Local JSON list of bounded field definitions; no inferred personal values",
            )
            cmd.add_argument(
                "--flatten", action="store_true", help="Flatten filled widgets into page appearance"
            )
        if operation == "redact":
            cmd.add_argument(
                "--redact-text",
                default="",
                help="Exact native text to remove (not OCR text in images)",
            )
            cmd.add_argument(
                "--redact-boxes-json",
                type=Path,
                help="Local JSON list of {page, rect:[x0,y0,x1,y1]}",
            )
        if operation == "rotate":
            cmd.add_argument("--rotation", type=int, choices=(90, 180, 270), default=90)
        if operation == "render":
            cmd.add_argument("--dpi", type=int, default=120)
        if operation in {"imagepdf", "merge"}:
            cmd.add_argument(
                "--dpi",
                type=int,
                default=144,
                help="Image page size in pixels per inch; does not resample pixels",
            )
        if operation == "compress":
            cmd.add_argument(
                "--compress-profile",
                choices=("screen", "ebook", "printer", "prepress"),
                default="ebook",
            )
            cmd.add_argument(
                "--allow-lossy",
                action="store_true",
                help="Acknowledge rewrite may lose forms/tags/layers/attachments/signatures",
            )
        if operation == "docling":
            cmd.add_argument("--no-ocr", dest="ocr", action="store_false")
    from .catalog import OPERATIONS

    for operation, contract in OPERATIONS.items():
        cmd = commands.add_parser(operation, help=contract["label"])
        cmd.add_argument("inputs", nargs="+" if contract["inputs"] else "*", type=Path)
        cmd.add_argument("-o", "--output", type=Path, default=Path("output"))
        if contract["inputs"]:
            cmd.add_argument("--ask-password", action="store_true")
        if contract["pages"]:
            cmd.add_argument("--pages", default="")
        for key, spec in contract["options"].items():
            kwargs = {"default": spec["default"], "help": spec["label"]}
            if spec["type"] == "boolean":
                kwargs["action"] = "store_true"
            else:
                kwargs["type"] = {"number": float, "integer": int}.get(spec["type"], str)
                if "enum" in spec:
                    kwargs["choices"] = spec["enum"]
            cmd.add_argument("--" + key.replace("_", "-"), **kwargs)
    return root


def main(argv: list[str] | None = None) -> int:
    os.environ["UV_PYTHON_DOWNLOADS"] = "never"
    args = parser().parse_args(argv)
    if args.command == "gui":
        from .server import serve

        serve(args.port, not args.no_browser)
        return 0
    if args.command == "doctor":
        print(
            json.dumps(
                {
                    "engines": engines(),
                    "limits": asdict(Limits()),
                    "python": sys.version.split()[0],
                },
                indent=2,
            )
        )
        return 0
    input_temp = None
    try:
        candidates = []
        for candidate in args.inputs:
            if candidate.is_dir():
                root = candidate.resolve()
                for directory, subdirs, names in os.walk(root, followlinks=False):
                    depth = len(Path(directory).relative_to(root).parts)
                    subdirs[:] = (
                        sorted(d for d in subdirs if not (Path(directory) / d).is_symlink())
                        if depth < 8
                        else []
                    )
                    for name in sorted(names):
                        path = Path(directory) / name
                        if not path.is_symlink() and path.suffix.lower() in {
                            ".pdf",
                            ".html",
                            ".htm",
                            ".zip",
                            ".tar",
                            ".gz",
                            ".tgz",
                            ".jpg",
                            ".jpeg",
                            ".png",
                            ".jp2",
                            ".jpx",
                            ".j2k",
                        }:
                            candidates.append(path)
                            if len(candidates) > Limits().max_files:
                                raise JobError("Folder intake exceeds the file-count limit")
            else:
                candidates.append(candidate)
        paths = [] if args.command == "create" and not candidates else validate_inputs(candidates)
        args.output.mkdir(parents=True, exist_ok=True)
        files = [
            {"id": str(i), "name": path.name, "path": str(path)} for i, path in enumerate(paths)
        ]
        import_warnings = []
        if args.command not in {"unpack", "bundle"} and any(
            p.suffix.lower() in {".zip", ".tar", ".gz", ".tgz"} for p in paths
        ):
            from dataclasses import replace

            input_temp = tempfile.TemporaryDirectory(prefix="pdf-multitool-cli-import-")
            files = []
            expanded_bytes = 0
            for index, path in enumerate(paths):
                if path.suffix.lower() in {".zip", ".tar", ".gz", ".tgz"}:
                    staging = Path(input_temp.name) / f"archive-{index:04d}"
                    remaining = Limits().max_session_bytes - expanded_bytes
                    report = run_worker(
                        {
                            "operation": "unpack",
                            "inputs": [str(path)],
                            "names": [path.name],
                            "options": {
                                "max_expanded_bytes": remaining,
                                "max_files": Limits().max_files - len(files),
                            },
                        },
                        staging,
                        threading.Event(),
                        replace(Limits(), timeout_seconds=35, max_output_bytes=remaining),
                    )
                    import_warnings.extend(report.get("warnings", []))
                    for output in report["outputs"]:
                        result_path = (staging / output["file"]).resolve()
                        if not result_path.is_relative_to(staging.resolve()):
                            raise JobError("Archive result escaped its staging directory")
                        files.append(
                            {
                                "id": str(len(files)),
                                "name": output["name"],
                                "path": str(result_path),
                            }
                        )
                        expanded_bytes += result_path.stat().st_size
                else:
                    files.append({"id": str(len(files)), "name": path.name, "path": str(path)})
                    expanded_bytes += path.stat().st_size
                if len(files) > Limits().max_files or expanded_bytes > Limits().max_session_bytes:
                    raise JobError("Expanded inputs exceed the session limit")
            if not files:
                raise JobError("No supported documents were found in the selected archives")
        options = {k: v for k, v in vars(args).items() if k not in {"command", "inputs", "output"}}
        import getpass

        if options.pop("ask_password", False):
            options["password"] = getpass.getpass("Document password: ")
        if args.command == "protect":
            options["new_password"] = getpass.getpass("New PDF password (8-127 UTF-8 bytes): ")
            if options["new_password"] != getpass.getpass("Confirm new PDF password: "):
                raise JobError("New passwords do not match")
        for arg, target in (
            ("fields_json", "fields"),
            ("create_fields_json", "create_fields"),
            ("redact_boxes_json", "redact_boxes"),
        ):
            file = options.pop(arg, None)
            if file:
                options[target] = read_local_json(file)
        job = Job(args.command, files, options, args.output.resolve())
        job.warnings.extend(import_warnings)
        previous = signal.signal(signal.SIGINT, lambda *_: job.cancel_event.set())
        try:
            job.run()
        finally:
            signal.signal(signal.SIGINT, previous)
        report = job.snapshot()
        report["output_directory"] = str(job.directory)
        for public, internal in zip(report["results"], job.results, strict=True):
            public.pop("url", None)
            public.pop("text", None)
            public["path"] = internal["path"]
        report_path = job.directory / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return {"completed": 0, "partial": 2, "cancelled": 130}.get(job.state, 1)
    except (JobError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if input_temp is not None:
            input_temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
