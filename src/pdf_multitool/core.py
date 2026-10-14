"""Portable job protocol and bounded process supervisor, with no UI dependencies."""
from __future__ import annotations

from dataclasses import dataclass, asdict, replace
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid


@dataclass(frozen=True)
class Limits:
    max_files: int = 200
    max_file_bytes: int = 50 * 1024 * 1024
    max_session_bytes: int = 500 * 1024 * 1024
    max_output_bytes: int = 500 * 1024 * 1024
    max_pages: int = 2000
    max_result_files: int = 1000
    timeout_seconds: int = 120
    docling_timeout_seconds: int = 300
    memory_mb: int = 2048
    docling_memory_mb: int = 6144
    max_log_bytes: int = 1024 * 1024


class JobError(Exception):
    pass


class Cancelled(JobError):
    pass


def strict_json_loads(value: str | bytes):
    """Reject duplicate keys and non-JSON numeric constants at public boundaries."""
    def object_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise JobError("Duplicate JSON object key")
            result[key] = item
        return result

    def constant(_):
        raise JobError("JSON must not contain non-finite numbers")

    try:
        parsed = json.loads(value, object_pairs_hook=object_pairs, parse_constant=constant)
    except RecursionError as exc:
        raise JobError("JSON nesting exceeds the depth limit") from exc
    pending = [(parsed, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise JobError("JSON nesting exceeds the depth limit")
        if isinstance(item, float) and not math.isfinite(item):
            raise JobError("JSON must not contain non-finite numbers")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return parsed


def read_local_json(path: Path, maximum: int = 128 * 1024):
    """Read a bounded regular local configuration file, never a FIFO or device."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise JobError("Configuration JSON must be a regular local file")
        if info.st_size > maximum:
            raise JobError("Configuration JSON is too large")
        with os.fdopen(fd, "rb", closefd=False) as file:
            data = file.read(maximum + 1)
        if len(data) > maximum:
            raise JobError("Configuration JSON is too large")
        return strict_json_loads(data.decode("utf-8-sig"))
    finally:
        os.close(fd)


def directory_size(directory: Path, max_entries: int = 100_000) -> int:
    """Bounded quota scan tolerating only files removed during enumeration."""
    total = 0
    for index, path in enumerate(directory.rglob("*"), 1):
        if index > max_entries:
            raise JobError("Workspace file-count limit exceeded during quota check")
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            # Native font caches atomically rename/remove temporary files.
            continue
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def safe_name(name: str) -> str:
    """Keep a legible name, never a caller-controlled path or control character."""
    name = re.split(r"[/\\]", name)[-1]
    name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)
    name = name.strip(" .")
    if not name:
        return "document.pdf"
    extension = Path(name).suffix
    for compound in (".tar.gz", ".pdf.gz", ".html.gz", ".htm.gz", ".jpg.gz", ".jpeg.gz", ".png.gz", ".jp2.gz", ".jpx.gz", ".j2k.gz"):
        if name.lower().endswith(compound):
            extension = name[-len(compound):]
            break
    if len(extension) > 16 or not re.fullmatch(r"\.[A-Za-z0-9.]+", extension):
        extension = ""
    stem = name[:-len(extension)] if extension else name
    budget = 180 - len(extension.encode("utf-8"))
    stem = stem.encode("utf-8", "ignore")[:budget].decode("utf-8", "ignore").rstrip(" .")
    return (stem or "document") + extension


def parse_pages(value: str, count: int) -> list[int]:
    """One-based inclusive ranges to unique zero-based page indices, in user order."""
    if not value or value.strip().lower() == "all":
        return list(range(count))
    if len(value) > 4096:
        raise JobError("Page selection is too long")
    result: list[int] = []
    seen: set[int] = set()
    for piece in value.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", piece)
        if not match:
            raise JobError("Use pages such as 1,3-5 (one-based page numbers)")
        start, end = int(match[1]), int(match[2] or match[1])
        if not 1 <= start <= end <= count:
            raise JobError(f"Page range {piece.strip()} is outside 1-{count}")
        for page in range(start - 1, end):
            if page not in seen:
                seen.add(page)
                result.append(page)
    return result


def _package(name: str) -> dict:
    try:
        return {"available": True, "version": importlib.metadata.version(name),
                "provenance": "Check docs/provenance.md for installation evidence"}
    except importlib.metadata.PackageNotFoundError:
        return {"available": False, "version": None, "reason": f"{name} is not installed"}


def _binary(name: str, version_arg: str) -> dict:
    path = shutil.which(name)
    if not path:
        return {"available": False, "version": None, "reason": f"{name} is not installed"}
    try:
        # Version arguments cannot touch document files. Keep output and time bounded.
        proc = subprocess.run([path, version_arg], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=5, check=False)
        version = proc.stdout[:2048].decode("utf-8", "replace").splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError) as exc:
        return {"available": False, "version": None, "reason": str(exc)}
    return {"available": True, "version": version, "path": path,
            "provenance": "Preinstalled; upstream artifact hash not verified by this app"}


def engines() -> dict:
    docling = _package("docling")
    docling["installed"] = docling["available"]
    if docling["available"]:
        try:
            from .models import validate_model_manifest
            validate_model_manifest(os.environ.get("PDF_MULTITOOL_MODELS"))
        except (JobError, OSError, ValueError) as exc:
            docling.update(available=False, reason=str(exc))
    poppler = _binary("pdftoppm", "-v")
    poppler["text_engine"] = _binary("pdftotext", "-v")
    if not shutil.which("pdftotext"):
        poppler.update(available=False, reason="pdftotext is not installed")
    pillow = _package("Pillow")
    if pillow["available"]:
        try:
            from PIL import features
            pillow["codecs"] = {"JPEG": features.check("jpg"), "PNG": features.check("zlib"),
                                "JPEG2000": features.check("jpg_2000")}
        except ImportError:
            pillow.update(available=False, reason="Pillow codec inspection failed")
    return {"pymupdf": _package("PyMuPDF"), "pillow": pillow, "poppler": poppler,
            "ghostscript": _binary("gs", "--version"),
            "weasyprint": _package("weasyprint"), "docling": docling}


def _terminate(proc: subprocess.Popen) -> None:
    # Kill the whole session even if its leader exited while a descendant survived.
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
        return
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def run_worker(payload: dict, workdir: Path, cancel: threading.Event,
               limits: Limits = Limits()) -> dict:
    """All document parsing happens in a disposable process group."""
    workdir.mkdir(parents=True, exist_ok=False, mode=0o700)
    payload = {**payload, "workdir": str(workdir.resolve()), "limits": asdict(limits)}
    encoded_payload = json.dumps(payload).encode("utf-8")
    if len(encoded_payload) > 256 * 1024:
        raise JobError("Job configuration exceeds its size limit")
    # Engines do not need cloud API tokens, Git credentials or unrelated app secrets.
    allowed_env = {"PATH", "PYTHONHOME", "LD_LIBRARY_PATH", "LANG", "LC_ALL", "LC_CTYPE",
                   "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "PDF_MULTITOOL_MODELS"}
    env = {key: value for key, value in os.environ.items() if key in allowed_env}
    env["HOME"] = str(workdir.resolve())
    env.update(UV_PYTHON_DOWNLOADS="never", HF_HUB_OFFLINE="1",
               TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1",
               DOCLING_SERVE_ENABLE_REMOTE_SERVICES="false", OMP_NUM_THREADS="2",
               OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2")
    # Source checkout launch works even when the caller's current directory differs.
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
    timeout = (limits.docling_timeout_seconds if payload["operation"] == "docling"
               else limits.timeout_seconds)
    log_path = workdir / "worker.log"
    with log_path.open("wb") as log:
        os.chmod(log_path, 0o600)
        proc = subprocess.Popen([sys.executable, "-m", "pdf_multitool.worker"],
                                cwd=workdir, env=env, stdin=subprocess.PIPE,
                                stdout=log, stderr=log, start_new_session=os.name == "posix")
        started = time.monotonic()
        try:
            # Passwords and form values travel over an anonymous pipe, never argv or disk.
            proc.stdin.write(encoded_payload)
            proc.stdin.close()
            while proc.poll() is None:
                if cancel.wait(0.06):
                    raise Cancelled("Cancelled; completed files remain available")
                if time.monotonic() - started > timeout:
                    raise JobError(f"Processing exceeded {timeout} seconds")
                if log_path.stat().st_size > limits.max_log_bytes:
                    raise JobError("Engine diagnostic output exceeded its limit")
                total = directory_size(workdir)
                if total > limits.max_output_bytes:
                    raise JobError("Output exceeded the configured size limit")
        finally:
            _terminate(proc)
    if log_path.stat().st_size > limits.max_log_bytes:
        raise JobError("Engine diagnostic output exceeded its limit")
    if directory_size(workdir) > limits.max_output_bytes:
        raise JobError("Output exceeded the configured size limit")
    result_path = workdir / "result.json"
    if not result_path.is_file():
        with log_path.open("rb") as log:
            log.seek(max(0, log_path.stat().st_size - 2000))
            diagnostic = log.read(2000).decode("utf-8", "replace")
        raise JobError(f"Engine stopped without a result (exit {proc.returncode}): {diagnostic}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not result.get("ok"):
        raise JobError(result.get("error", "Engine failed"))
    return result


def validate_inputs(paths: list[Path], limits: Limits = Limits()) -> list[Path]:
    if not paths or len(paths) > limits.max_files:
        raise JobError(f"Choose between 1 and {limits.max_files} files")
    valid: list[Path] = []
    total = 0
    for path in paths:
        path = path.resolve(strict=True)
        if not path.is_file():
            raise JobError(f"Not a regular file: {path.name}")
        size = path.stat().st_size
        if size > limits.max_file_bytes:
            raise JobError(f"{path.name}: exceeds {limits.max_file_bytes // (1024 * 1024)} MB")
        total += size
        valid.append(path)
    if total > limits.max_session_bytes:
        raise JobError("Selected files exceed the session size limit")
    return valid


class Job:
    def __init__(self, operation: str, files: list[dict], options: dict, directory: Path,
                 limits: Limits = Limits()):
        if operation not in {"merge", "split", "rotate", "render", "text", "html", "compress", "docling", "preview", "identify", "pdfhtml", "rename", "repaginate", "clean", "forms", "signatures", "redact", "unlock", "protect", "unpack", "bundle", "markdown", "textlayout", "imagepdf"}:
            raise JobError("Unknown operation")
        if not isinstance(options, dict):
            raise JobError("Options must be an object")
        self.id = uuid.uuid4().hex
        self.operation, self.files, self.options = operation, files, options
        self.directory, self.limits = directory / self.id, limits
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.cancel_event = threading.Event()
        self.lock = threading.RLock()
        self.state = "queued"
        self.progress = 0.0
        self.message = "Waiting"
        self.results: list[dict] = []
        self.errors: list[dict] = []
        self.warnings: list[str] = []

    def snapshot(self) -> dict:
        with self.lock:
            return {"id": self.id, "state": self.state, "operation": self.operation,
                    "progress": self.progress, "message": self.message,
                    "results": [{k: v for k, v in item.items() if k != "path"} for item in self.results],
                    "errors": list(self.errors), "warnings": list(self.warnings)}

    def run(self) -> None:
        with self.lock:
            self.state, self.message = "running", "Starting"
        groups = [self.files] if self.operation in {"merge", "bundle", "imagepdf"} else [[f] for f in self.files]
        try:
            for index, group in enumerate(groups):
                if self.cancel_event.is_set():
                    raise Cancelled("Cancelled; completed files remain available")
                label = ", ".join(f["name"] for f in group)
                with self.lock:
                    self.message = f"Processing {label}"
                workdir = self.directory / f"part-{index + 1:04d}"
                try:
                    if len(self.results) >= self.limits.max_result_files:
                        raise JobError("Job result file-count limit reached")
                    used = directory_size(self.directory)
                    remaining = self.limits.max_output_bytes - used
                    if remaining <= 0:
                        raise JobError("Job output size limit reached")
                    result = run_worker({"operation": self.operation,
                                         "inputs": [str(f["path"]) for f in group],
                                         "names": [f["name"] for f in group],
                                         "archive_paths": [f.get("archive_path", f["name"]) for f in group],
                                         "options": self.options}, workdir, self.cancel_event,
                                        replace(self.limits, max_output_bytes=remaining))
                    with self.lock:
                        if len(self.results) + len(result["outputs"]) > self.limits.max_result_files:
                            raise JobError("Job result file-count limit reached")
                        # Publish a part only after every output has been validated.
                        # A later failure removes this part, so no records may escape early.
                        records = []
                        warnings = []
                        for output in result["outputs"]:
                            path = (workdir / output["file"]).resolve()
                            if not path.is_relative_to(workdir.resolve()) or not path.is_file() or path.is_symlink():
                                raise JobError("Invalid engine output location")
                            relative = path.relative_to(workdir.resolve())
                            if relative.parent == Path("."):
                                archive_name = safe_name(output["name"]).replace(":", "_")
                                if archive_name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
                                    archive_name = "Document " + archive_name
                                if archive_name != output["name"]:
                                    warnings.append("A result filename was made archive-safe; its displayed download name remains available separately.")
                                relative = Path(archive_name)
                            result_id = uuid.uuid4().hex
                            record = {"id": result_id, "name": output["name"],
                                      "size": path.stat().st_size, "path": str(path),
                                      "bundle_path": f"document-{index + 1:04d}/{relative.as_posix()}",
                                      "url": f"/api/results/{self.id}/{result_id}"}
                            if path.suffix in {".txt", ".md"} and path.stat().st_size <= 2 * 1024 * 1024:
                                record["text"] = path.read_text(encoding="utf-8")
                            if "member_path" in output:
                                record["source_member_path"] = output["member_path"]
                            records.append(record)
                        warnings.extend(result.get("warnings", []))
                        self.results.extend(records)
                        self.warnings.extend(warnings)
                except Cancelled:
                    shutil.rmtree(workdir, ignore_errors=True)
                    raise
                except (JobError, OSError, ValueError) as exc:
                    with self.lock:
                        self.errors.append({"name": label, "error": str(exc)[:2000]})
                    shutil.rmtree(workdir, ignore_errors=True)
                with self.lock:
                    self.progress = (index + 1) / len(groups)
            with self.lock:
                self.state = "partial" if self.errors and self.results else "failed" if self.errors else "completed"
                self.message = {"partial": "Finished with some errors", "failed": "No files completed", "completed": "Finished"}[self.state]
                self.warnings = list(dict.fromkeys(self.warnings))
        except Cancelled as exc:
            with self.lock:
                self.state, self.message = "cancelled", str(exc)
        except Exception as exc:
            with self.lock:
                self.state, self.message = "failed", str(exc)[:2000]
        finally:
            self.clear_secrets()

    def clear_secrets(self) -> None:
        """Drop retained references; Python does not promise memory zeroization."""
        with self.lock:
            for key in ("password", "new_password", "fields", "create_fields"):
                self.options.pop(key, None)
