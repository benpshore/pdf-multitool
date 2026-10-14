"""Single-user, loopback-only browser workbench. Not an internet hosting service."""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import secrets
import shutil
import tempfile
import threading
import urllib.parse
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import (
    DEFAULT_LIMITS,
    Job,
    JobError,
    Limits,
    directory_size,
    engines,
    run_worker,
    safe_name,
    strict_json_loads,
)


class Workbench:
    def __init__(self, directory: Path, limits: Limits = DEFAULT_LIMITS):
        self.directory, self.limits = directory, limits
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.files: dict[str, dict] = {}
        self.archives: dict[str, dict] = {}
        self.pending_uploads: dict[str, int] = {}
        self.jobs: dict[str, Job] = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-job")
        self.preview_lock = threading.Lock()
        self.engine_lock = threading.Lock()
        self.closed = threading.Event()
        self.engine_status = engines()
        (directory / "inputs").mkdir()
        (directory / "jobs").mkdir()
        (directory / "previews").mkdir()
        (directory / "imports").mkdir()

    def run_job(self, job: Job) -> None:
        with self.engine_lock:
            if self.closed.is_set():
                job.cancel_event.set()
            job.run()

    def expand_archive(self, target: Path, name: str, file_id: str, length: int) -> dict:
        if not self.engine_lock.acquire(blocking=False):
            raise JobError("Processing is active; wait before importing an archive")
        staging = self.directory / "imports" / file_id
        created = []
        try:
            if self.closed.is_set():
                raise JobError("Workspace is closing")
            with self.lock:
                used = sum(f["size"] for f in self.files.values()) + sum(
                    a["size"] for a in self.archives.values()
                )
                available = (
                    self.limits.max_session_bytes - used - sum(self.pending_uploads.values())
                )
                count = self.limits.max_files - len(self.files) - len(self.pending_uploads) + 1
                if available <= 0 or count <= 0:
                    raise JobError("No session capacity remains for expanded archive files")
                # Reserve the full possible expansion so concurrent uploads cannot race it.
                self.pending_uploads[file_id] = length + available
            result = run_worker(
                {
                    "operation": "unpack",
                    "inputs": [str(target)],
                    "names": [name],
                    "options": {"max_expanded_bytes": available, "max_files": count},
                },
                staging,
                self.closed,
                replace(self.limits, timeout_seconds=35, max_output_bytes=available),
            )
            if not result["outputs"]:
                raise JobError("Archive contained no supported PDF, HTML or image files")
            entries = []
            for output in result["outputs"]:
                path = (staging / output["file"]).resolve(strict=True)
                if not path.is_relative_to(staging.resolve()) or not path.is_file():
                    raise JobError("Invalid extracted file location")
                new_id = uuid.uuid4().hex
                destination = (
                    self.directory / "inputs" / (new_id + Path(output["name"]).suffix.lower())
                )
                if destination.exists():
                    raise JobError("Unexpected input filename collision")
                path.rename(destination)
                created.append(destination)
                entries.append(
                    {
                        "id": new_id,
                        "name": output["name"],
                        "size": destination.stat().st_size,
                        "path": str(destination),
                        "member_path": output.get("member_path", output["name"]),
                        "source_archive": {
                            "id": file_id,
                            "name": name,
                            "url": f"/api/archives/{file_id}/original",
                        },
                    }
                )
            with self.lock:
                for entry in entries:
                    self.files[entry["id"]] = entry
                self.archives[file_id] = {
                    "id": file_id,
                    "name": name,
                    "size": length,
                    "path": str(target),
                }
            return {
                "files": [{k: v for k, v in entry.items() if k != "path"} for entry in entries],
                "warnings": result.get("warnings", []),
                "statistics": result.get("statistics", {}),
            }
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            self.engine_lock.release()

    def file_list(self) -> list[dict]:
        with self.lock:
            return [{k: v for k, v in item.items() if k != "path"} for item in self.files.values()]

    def close(self) -> None:
        self.closed.set()
        with self.lock:
            for job in self.jobs.values():
                job.cancel_event.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
        # Imports/previews use the same gate; wait for their cancelled workers.
        with self.engine_lock:
            pass
        with self.lock:
            for job in self.jobs.values():
                job.clear_secrets()


class BoundedHTTPServer(ThreadingHTTPServer):
    """Reject excess connections before creating a thread or accepting a body."""

    daemon_threads = True
    request_queue_size = 8

    def __init__(self, *args, **kwargs):
        self.request_slots = threading.BoundedSemaphore(4)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


def make_handler(app: Workbench):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PDFMultitool"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            # No paths, filenames, tokens or document text in ordinary access logs.
            pass

        def end_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                (
                    "default-src 'none'; script-src 'self'; style-src 'self'; img-src "
                    "'self' blob:; connect-src 'self'; font-src 'self'; base-uri "
                    "'none'; form-action 'none'; frame-ancestors 'none'"
                ),
            )
            super().end_headers()

        def send_json(self, data: dict, status: int = 200):
            content = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def allowed(self, token=True) -> bool:
            if app.closed.is_set():
                self.send_json({"error": "Workspace is closing"}, 503)
                return False
            expected = f"127.0.0.1:{self.server.server_port}"
            host = self.headers.get("Host", "")
            if host != expected:
                self.send_json({"error": "Invalid Host; use the exact launch URL"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{expected}":
                self.send_json({"error": "Cross-origin requests are not allowed"}, 403)
                return False
            if token and not hmac.compare_digest(self.headers.get("X-PDF-Token", ""), app.token):
                self.send_json({"error": "Session token required; reopen the launch URL"}, 401)
                return False
            return True

        def body_length(self, maximum: int) -> int:
            if self.headers.get("Transfer-Encoding"):
                raise JobError("Chunked upload is not supported")
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                raise JobError("Invalid Content-Length") from None
            if not 0 <= length <= maximum:
                raise JobError("Request exceeds the configured size limit")
            return length

        def read_body(self, maximum: int) -> bytes:
            length = self.body_length(maximum)
            body = self.rfile.read(length)
            if len(body) != length:
                raise JobError("Incomplete request body")
            return body

        def send_file(self, path: Path, name: str | None = None):
            size = path.stat().st_size
            self.send_response(200)
            self.send_header(
                "Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            self.send_header("Content-Length", str(size))
            if name:
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename*=UTF-8''" + urllib.parse.quote(name, safe=""),
                )
            self.end_headers()
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=64 * 1024)

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if not self.allowed(token=path.startswith("/api/")):
                return
            try:
                if path == "/api/status":
                    from dataclasses import asdict

                    self.send_json(
                        {
                            "engines": app.engine_status,
                            "limits": asdict(app.limits),
                            "mode": "local-development",
                            "storage": "temporary-session",
                        }
                    )
                elif path == "/api/files":
                    self.send_json({"files": app.file_list()})
                elif path.startswith("/api/archives/"):
                    parts = path.split("/")
                    if len(parts) != 5 or parts[4] != "original":
                        raise KeyError("Invalid archive route")
                    with app.lock:
                        archive = app.archives[parts[3]].copy()
                    self.send_file(Path(archive["path"]), archive["name"])
                elif path.startswith("/api/files/"):
                    parts = path.split("/")
                    if len(parts) != 5 or parts[4] not in {"original", "preview"}:
                        raise KeyError("Invalid file route")
                    with app.lock:
                        item = app.files[parts[3]].copy()
                    if parts[4] == "original":
                        self.send_file(Path(item["path"]), item["name"])
                    else:
                        with app.preview_lock:
                            preview = app.directory / "previews" / item["id"]
                            if not (preview / "preview.png").is_file():
                                if preview.exists():
                                    shutil.rmtree(preview)
                                if not app.engine_lock.acquire(blocking=False):
                                    raise JobError(
                                        "Processing is active; preview is available "
                                        "after it finishes"
                                    )
                                try:
                                    if app.closed.is_set():
                                        raise JobError("Workspace is closing")
                                    run_worker(
                                        {
                                            "operation": "preview",
                                            "inputs": [item["path"]],
                                            "names": [item["name"]],
                                            "options": {},
                                        },
                                        preview,
                                        app.closed,
                                        app.limits,
                                    )
                                finally:
                                    app.engine_lock.release()
                            self.send_file(preview / "preview.png")
                elif path.startswith("/api/jobs/"):
                    job_id = path.rsplit("/", 1)[1]
                    with app.lock:
                        job = app.jobs[job_id]
                    self.send_json(job.snapshot())
                elif path.startswith("/api/results/"):
                    parts = path.split("/")
                    if len(parts) != 5:
                        raise KeyError("Invalid result route")
                    with app.lock:
                        job = app.jobs[parts[3]]
                    with job.lock:
                        result = next(item.copy() for item in job.results if item["id"] == parts[4])
                    self.send_file(Path(result["path"]), result["name"])
                elif path in {"/", "/app.css", "/app.js", "/static/app.css", "/static/app.js"}:
                    static = Path(__file__).parent / "static"
                    self.send_file(
                        static / ("index.html" if path == "/" else path.rsplit("/", 1)[1])
                    )
                else:
                    self.send_json({"error": "Not found"}, 404)
            except KeyError, StopIteration, FileNotFoundError:
                self.send_json({"error": "Not found"}, 404)
            except (JobError, ValueError, OSError) as exc:
                self.send_json({"error": str(exc)[:2000]}, 400)

        def do_POST(self):
            if not self.allowed():
                self.close_connection = True
                return
            path = urllib.parse.urlsplit(self.path).path
            try:
                if path == "/api/files":
                    name = safe_name(
                        urllib.parse.unquote(self.headers.get("X-Filename", "document.pdf"))
                    )
                    suffix = Path(name).suffix.lower()
                    archive_input = suffix in {".zip", ".tar", ".gz", ".tgz"}
                    if suffix not in {
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
                        raise JobError(
                            "Choose PDF, HTML, JPEG, PNG, JPEG2000 or supported archive files"
                        )
                    length = self.body_length(app.limits.max_file_bytes)
                    if not length:
                        raise JobError("Empty file")
                    file_id = uuid.uuid4().hex
                    target = app.directory / "inputs" / (file_id + suffix)
                    with app.lock:
                        if len(app.files) + len(app.pending_uploads) >= app.limits.max_files:
                            raise JobError("Session file count limit reached")
                        total = (
                            sum(f["size"] for f in app.files.values())
                            + sum(a["size"] for a in app.archives.values())
                            + sum(app.pending_uploads.values())
                            + length
                        )
                        if total > app.limits.max_session_bytes:
                            raise JobError("Session upload size limit reached")
                        app.pending_uploads[file_id] = length
                    try:
                        with target.open("xb") as output:
                            os.chmod(target, 0o600)
                            remaining = length
                            while remaining:
                                chunk = self.rfile.read(min(65536, remaining))
                                if not chunk:
                                    raise JobError("Incomplete upload")
                                output.write(chunk)
                                remaining -= len(chunk)
                        if archive_input:
                            response = app.expand_archive(target, name, file_id, length)
                        else:
                            item = {
                                "id": file_id,
                                "name": name,
                                "size": length,
                                "path": str(target),
                            }
                            with app.lock:
                                app.files[file_id] = item
                            response = {k: v for k, v in item.items() if k != "path"}
                    except BaseException:
                        target.unlink(missing_ok=True)
                        raise
                    finally:
                        with app.lock:
                            app.pending_uploads.pop(file_id, None)
                    self.send_json(response, 201)
                elif path == "/api/bundles":
                    data = strict_json_loads(self.read_body(8192))
                    if not isinstance(data, dict) or data.get("format") not in {"zip", "tar.gz"}:
                        raise JobError("Bundle request requires a job and zip or tar.gz format")
                    with app.lock:
                        if any(j.state in {"queued", "running"} for j in app.jobs.values()):
                            raise JobError("Wait for active processing before packaging results")
                        if len(app.jobs) >= 100:
                            raise JobError("Session job limit reached")
                        source = app.jobs[data["job"]]
                        with source.lock:
                            files = [
                                {
                                    "id": item["id"],
                                    "name": item["name"],
                                    "path": item["path"],
                                    "archive_path": item["bundle_path"],
                                }
                                for item in source.results
                            ]
                        if not files:
                            raise JobError("No completed results to package")
                        used = directory_size(app.directory / "jobs")
                        remaining = app.limits.max_output_bytes - used
                        if remaining <= 0:
                            raise JobError("Session output limit reached")
                        job = Job(
                            "bundle",
                            files,
                            {"format": data["format"]},
                            app.directory / "jobs",
                            replace(app.limits, max_output_bytes=remaining),
                        )
                        app.jobs[job.id] = job
                        app.executor.submit(app.run_job, job)
                    self.send_json({"id": job.id}, 202)
                elif path == "/api/jobs":
                    data = strict_json_loads(self.read_body(32 * 1024))
                    if not isinstance(data, dict):
                        raise JobError("Job request must be a JSON object")
                    ids = data["files"]
                    if (
                        not isinstance(ids, list)
                        or not all(isinstance(v, str) for v in ids)
                        or not 1 <= len(ids) <= app.limits.max_files
                        or len(set(ids)) != len(ids)
                    ):
                        raise JobError("Choose distinct files for this job")
                    with app.lock:
                        if any(j.state in {"queued", "running"} for j in app.jobs.values()):
                            raise JobError("Wait for the active job or cancel it first")
                        if len(app.jobs) >= 100:
                            raise JobError("Session job limit reached; restart for a clean session")
                        used = directory_size(app.directory / "jobs")
                        remaining = app.limits.max_output_bytes - used
                        if remaining <= 0:
                            raise JobError(
                                "Session output limit reached; download results and restart"
                            )
                        files = [app.files[file_id].copy() for file_id in ids]
                        job = Job(
                            data["operation"],
                            files,
                            data.get("options", {}),
                            app.directory / "jobs",
                            replace(app.limits, max_output_bytes=remaining),
                        )
                        app.jobs[job.id] = job
                        app.executor.submit(app.run_job, job)
                    self.send_json({"id": job.id}, 202)
                elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                    parts = path.split("/")
                    if len(parts) != 5:
                        raise KeyError("Invalid cancel route")
                    self.read_body(1024)
                    with app.lock:
                        job = app.jobs[parts[3]]
                        job.cancel_event.set()
                    self.send_json(job.snapshot())
                else:
                    self.send_json({"error": "Not found"}, 404)
            except (JobError, ValueError, TypeError, KeyError, OSError) as exc:
                self.close_connection = True
                self.send_json({"error": str(exc)[:2000]}, 400)

        def do_DELETE(self):
            if not self.allowed():
                return
            path = urllib.parse.urlsplit(self.path).path
            try:
                if not path.startswith("/api/files/") or len(path.split("/")) != 4:
                    raise KeyError("Unknown route")
                file_id = path.rsplit("/", 1)[1]
                with app.lock:
                    if any(
                        j.state in {"queued", "running"}
                        and any(f["id"] == file_id for f in j.files)
                        for j in app.jobs.values()
                    ):
                        raise JobError("Cannot remove a file while its job is active")
                    item = app.files.pop(file_id)
                    Path(item["path"]).unlink(missing_ok=True)
                    shutil.rmtree(app.directory / "previews" / file_id, ignore_errors=True)
                    archive_id = item.get("source_archive", {}).get("id")
                    if archive_id and not any(
                        f.get("source_archive", {}).get("id") == archive_id
                        for f in app.files.values()
                    ):
                        archive = app.archives.pop(archive_id, None)
                        if archive:
                            Path(archive["path"]).unlink(missing_ok=True)
                self.send_json({"removed": True})
            except (KeyError, JobError, OSError) as exc:
                self.send_json({"error": str(exc)}, 400)

    return Handler


def serve(port: int = 0, open_browser: bool = True) -> None:
    with tempfile.TemporaryDirectory(prefix="pdf-multitool-") as temporary:
        os.chmod(temporary, 0o700)
        app = Workbench(Path(temporary))
        server = BoundedHTTPServer(("127.0.0.1", port), make_handler(app))
        server.daemon_threads = True
        url = f"http://127.0.0.1:{server.server_port}/#token={app.token}"
        print(f"PDF Multitool: {url}", flush=True)
        print(
            (
                "Single-user loopback session. Files are deleted when this server "
                "exits. Ctrl-C stops it."
            ),
            flush=True,
        )
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            app.close()
            server.server_close()
