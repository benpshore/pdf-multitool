"""Real CLI, loopback HTTP and Chromium contracts for the optional Oxide routes."""

import importlib.util
import json
import shutil
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import quote

import pymupdf
import pytest

from pdf_multitool import cli, oxide
from pdf_multitool.catalog import OPERATIONS
from pdf_multitool.server import BoundedHTTPServer, Workbench, make_handler
from tests.test_oxide_conversion import minimal_docx, minimal_other_office
from tests.test_oxide_editing import source_pdf

NATIVE = pytest.mark.skipif(not oxide.availability()["available"], reason="Optional bounded Oxide")
BROWSER = pytest.mark.skipif(
    not importlib.util.find_spec("playwright") or not shutil.which("chromium"),
    reason="Requires playwright and system Chromium",
)
NUMBERS = "Invoice 001 Total -12.50 Balance 0.00"
CREATION = [
    ("oxide_create_text", {"content": NUMBERS}, ".pdf"),
    (
        "oxide_create_document",
        {
            "document": json.dumps(
                {
                    "pages": [
                        {
                            "size": "a4",
                            "elements": [{"type": "text", "text": NUMBERS, "x": 72, "y": 740}],
                        }
                    ]
                }
            )
        },
        ".pdf",
    ),
    ("oxide_create_barcode", {"data": "Fixture001:-12.50"}, ".svg"),
    ("oxide_create_pattern", {}, ".txt"),
]


def command(*arguments):
    return subprocess.run(  # noqa: S603 -- current interpreter and explicit CLI arguments
        [sys.executable, "-m", "pdf_multitool", *map(str, arguments)],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def pdf_text(data, password=""):
    with pymupdf.open(stream=data, filetype="pdf") as document:
        if document.needs_pass:
            assert password and document.authenticate(password)
        return " ".join(page.get_text() for page in document)


def assert_numbers(text):
    assert all(value in text for value in ("001", "-12.50", "0.00")), text


class HTTP:
    def __init__(self, directory):
        self.app = Workbench(directory)
        self.server = BoundedHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.app.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        actual = {"X-PDF-Token": self.app.token, **(headers or {})}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            actual["Content-Type"] = "application/json"
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request(method, path, body, actual)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def upload(self, name, data):
        status, _, body = self.request("POST", "/api/files", data, {"X-Filename": quote(name)})
        assert status == 201, body
        return json.loads(body)

    def wait(self, job_id):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status, _, body = self.request("GET", f"/api/jobs/{job_id}")
            assert status == 200, body
            snapshot = json.loads(body)
            if snapshot["state"] not in {"queued", "running"}:
                return snapshot
            time.sleep(0.03)
        pytest.fail("Real HTTP job did not finish within 30 seconds")

    def submit(self, operation, files=(), options=None, *, state="completed"):
        status, _, body = self.request(
            "POST",
            "/api/jobs",
            {"operation": operation, "files": list(files), "options": options or {}},
        )
        assert status == 202, body
        snapshot = self.wait(json.loads(body)["id"])
        assert snapshot["state"] == state, snapshot
        return snapshot

    def download(self, job, suffix):
        results = [item for item in job["results"] if item["name"].endswith(suffix)]
        assert len(results) == 1, job
        status, headers, data = self.request("GET", results[0]["url"])
        assert status == 200 and "path" not in results[0]
        assert headers["Content-Disposition"].startswith("attachment;")
        assert headers["X-Content-Type-Options"] == "nosniff"
        return data


@pytest.fixture
def http(tmp_path):
    directory = tmp_path / "http"
    directory.mkdir()
    server = HTTP(directory)
    try:
        yield server
    finally:
        server.close()


@NATIVE
@pytest.mark.parametrize("operation,options,suffix", CREATION)
def test_cli_zero_input_creation_uses_actual_engine(tmp_path, operation, options, suffix):
    arguments = [operation, "-o", tmp_path / "cli-output"]
    for key, value in options.items():
        arguments.extend(["--" + key.replace("_", "-"), value])
    completed = command(*arguments)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    report = json.loads(completed.stdout)
    assert report["state"] == "completed"
    output = next(Path(item["path"]) for item in report["results"] if item["path"].endswith(suffix))
    if suffix == ".pdf":
        assert_numbers(pdf_text(output.read_bytes()))
    elif suffix == ".svg":
        assert "<svg" in output.read_text()
    else:
        assert output.read_text().strip()


def test_boolean_cli_defaults_are_preserved_and_can_be_disabled():
    for name, contract in OPERATIONS.items():
        if not name.startswith("oxide"):
            continue
        base = [name, *(["synthetic.pdf"] if contract["inputs"] else [])]
        arguments = vars(cli.parser().parse_args(base))
        for key, spec in contract["options"].items():
            if spec["type"] == "boolean":
                assert arguments[key] is spec["default"]
                disabled = cli.parser().parse_args([*base, "--no-" + key.replace("_", "-")])
                enabled = cli.parser().parse_args([*base, "--" + key.replace("_", "-")])
                assert getattr(disabled, key) is False and getattr(enabled, key) is True


def test_secret_cli_options_are_prompt_only(capsys):
    arguments = cli.parser().parse_args(
        ["oxide_security", "synthetic.pdf", "--ask-new-password", "--ask-owner-password"]
    )
    assert arguments.ask_new_password and arguments.ask_owner_password
    for flag in ("--new-password", "--owner-password", "--password"):
        with pytest.raises(SystemExit):
            cli.parser().parse_args(["oxide_security", "synthetic.pdf", flag, "public-fixture"])
    capsys.readouterr()


@NATIVE
def test_cli_private_prompt_values_reach_worker_but_not_argv_or_reports(
    tmp_path, monkeypatch, capsys
):
    source = source_pdf(tmp_path)
    values = ["Synthetic-user-123", "Synthetic-owner-456"]
    prompts = []

    def prompt(label):
        prompts.append(label)
        return values[len(prompts) - 1]

    monkeypatch.setattr("getpass.getpass", prompt)
    spawn = subprocess.Popen
    worker_arguments = []

    def observe(arguments, **kwargs):
        worker_arguments.append(arguments)
        return spawn(arguments, **kwargs)

    monkeypatch.setattr("pdf_multitool.core.subprocess.Popen", observe)
    status = cli.main(
        [
            "oxide_security",
            str(source),
            "--mode",
            "encrypt",
            "--ask-new-password",
            "--ask-owner-password",
            "-o",
            str(tmp_path / "private-output"),
        ]
    )
    captured = capsys.readouterr()
    # Native 0.3.78 AES output lacks a complete /Perms dictionary. Independent
    # qpdf validation must reject it even though MuPDF can reopen those bytes.
    assert status == 1, captured.out + captured.err
    assert len(prompts) == 2 and worker_arguments
    assert all(
        value not in repr(worker_arguments) + captured.out + captured.err for value in values
    )
    report = json.loads(captured.out)
    assert report["state"] == "failed" and report["results"] == []
    assert "independent PDF open" in str(report["errors"])
    written = list(Path(report["output_directory"]).iterdir())
    assert [path.name for path in written] == ["report.json"]
    assert all(value not in written[0].read_text() for value in values)
    assert "Invoice001Total1.25" in "".join(pdf_text(source.read_bytes()).split())


@NATIVE
def test_doctor_and_http_expose_actual_optional_engine_and_legacy_defaults(http, tmp_path):
    report = command("doctor")
    assert report.returncode == 0, report.stderr
    status, _, body = http.request("GET", "/api/status")
    assert status == 200
    public = json.loads(body)
    assert public["engines"]["pdf_oxide"] == json.loads(report.stdout)["engines"]["pdf_oxide"]
    assert public["engines"]["pdf_oxide"]["version"] == "0.3.78"
    assert public["engines"]["pymupdf"]["available"]
    assert public["capabilities"]["operations"]["oxide_create_text"]["inputs"] == 0
    source = source_pdf(tmp_path)
    before = source.read_bytes()
    item = http.upload(source.name, before)
    legacy = http.submit("text", [item["id"]])
    assert "Invoice 001 Total 1.25" in http.download(legacy, ".txt").decode()
    assert http.request("GET", f"/api/files/{item['id']}/original")[2] == before
    assert cli.parser().parse_args(["markdown", str(source)]).reading_order == "auto"
    assert cli.parser().parse_args(["render", str(source)]).dpi == 120


@NATIVE
def test_http_zero_input_routes_and_body_size_boundaries(http):
    for operation, options, suffix in CREATION:
        job = http.submit(operation, options=options)
        data = http.download(job, suffix)
        assert data
        if suffix == ".pdf":
            assert_numbers(pdf_text(data))
    request = {"operation": "oxide_create_text", "files": [], "options": {"content": NUMBERS}}
    encoded = json.dumps(request).encode().rjust(40 * 1024, b" ")
    status, _, body = http.request("POST", "/api/jobs", encoded)
    assert status == 202, body
    completed = http.wait(json.loads(body)["id"])
    assert completed["state"] == "completed", completed
    assert_numbers(pdf_text(http.download(completed, ".pdf")))
    jobs_before = len(http.app.jobs)
    request["operation"] = "create"
    status, _, body = http.request(
        "POST", "/api/jobs", json.dumps(request).encode().rjust(40 * 1024, b" ")
    )
    assert status == 400 and b"32 KiB" in body
    status, _, body = http.request(
        "POST", "/api/jobs", b"{}", {"Content-Length": str(256 * 1024 + 1)}
    )
    assert status == 400 and b"size limit" in body
    assert len(http.app.jobs) == jobs_before


@NATIVE
@pytest.mark.parametrize("kind", ["docx", "xlsx", "pptx"])
def test_office_upload_conversion_and_original_download(http, kind):
    original = minimal_docx() if kind == "docx" else minimal_other_office(kind)
    item = http.upload(f"public-fixture.{kind}", original)
    job = http.submit("oxide_office_import", [item["id"]])
    text = pdf_text(http.download(job, ".pdf"))
    assert "012" in text and "0.15" in text and "-2" in text
    assert http.request("GET", f"/api/files/{item['id']}/original")[2] == original


@NATIVE
def test_http_secret_values_clear_and_failed_operation_publishes_nothing(http, tmp_path):
    source = source_pdf(tmp_path)
    original = source.read_bytes()
    item = http.upload(source.name, original)
    secret = "Synthetic-browser-value"  # noqa: S105 -- public synthetic test password
    options = {"mode": "encrypt", "new_password": secret, "owner_password": "Synthetic-owner-value"}
    job = http.submit("oxide_security", [item["id"]], options, state="failed")
    assert "independent PDF open" in str(job["errors"])
    assert job["results"] == []
    internal = http.app.jobs[job["id"]]
    assert list(internal.directory.iterdir()) == []
    assert "new_password" not in internal.options and "owner_password" not in internal.options
    assert secret not in json.dumps(job)
    for result in job["results"]:
        if result["name"].endswith(".json"):
            assert secret.encode() not in http.request("GET", result["url"])[2]
    before = len(http.app.jobs)
    status, _, body = http.request(
        "POST",
        "/api/jobs",
        {
            "operation": "oxide_edit_text",
            "files": [item["id"]],
            "options": {"action": "replace", "find": "Invoice", "text": "Receipt"},
        },
    )
    assert status == 400 and b"black mask" in body
    assert len(http.app.jobs) == before
    assert http.request("GET", f"/api/files/{item['id']}/original")[2] == original


@NATIVE
@BROWSER
def test_browser_zero_input_downloads_and_secret_controls(http, tmp_path):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=shutil.which("chromium"), args=["--no-sandbox"]
        )
        try:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{http.base}/#token={http.app.token}")
            source = source_pdf(tmp_path)
            original = source.read_bytes()
            page.locator("#file-input").set_input_files(source)
            expect(page.locator("#file-count")).to_have_text("1")
            page.select_option("#operation", "oxide_edit_merge")
            expect(page.locator("#run-button")).to_be_disabled()
            for operation, options, suffix in CREATION:
                page.select_option("#operation", operation)
                expect(page.locator("#password-field")).to_be_hidden()
                for key, value in options.items():
                    page.locator(f"#option-{key}").fill(value)
                expect(page.locator("#run-button")).to_be_enabled()
                with page.expect_response(
                    lambda response: (
                        response.url.endswith("/api/jobs") and response.request.method == "POST"
                    )
                ) as event:
                    page.locator("#run-button").click()
                assert event.value.status == 202
                job_id = event.value.json()["id"]
                expect(page.locator("#run-button")).to_be_enabled(timeout=20000)
                job = http.app.jobs[job_id]
                assert job.state == "completed" and job.files == [], job.snapshot()
                output = next(item for item in job.results if item["name"].endswith(suffix))
                with page.expect_download() as download:
                    page.get_by_role(
                        "button", name=f"Download {output['name']}", exact=True
                    ).click()
                downloaded = Path(download.value.path()).read_bytes()
                assert downloaded == Path(output["path"]).read_bytes()
                if suffix == ".pdf":
                    assert_numbers(pdf_text(downloaded))
            page.select_option("#operation", "oxide_security")
            page.select_option("#option-mode", "encrypt")
            for field in ("new_password", "owner_password"):
                assert page.locator(f"#option-{field}").get_attribute("type") == "password"
            page.locator("#option-new_password").fill("Synthetic-browser-user")
            page.locator("#option-owner_password").fill("Synthetic-browser-owner")
            with page.expect_response(
                lambda response: (
                    response.url.endswith("/api/jobs") and response.request.method == "POST"
                )
            ) as event:
                page.locator("#run-button").click()
            assert event.value.status == 202
            expect(page.locator("#option-new_password")).to_have_value("")
            expect(page.locator("#option-owner_password")).to_have_value("")
            expect(page.locator("#run-button")).to_be_enabled(timeout=20000)
            job = http.app.jobs[event.value.json()["id"]]
            assert job.state == "failed" and job.results == [], job.snapshot()
            assert "independent PDF open" in str(job.errors)
            assert list(job.directory.iterdir()) == []
            assert "Synthetic-browser" not in page.evaluate("JSON.stringify(sessionStorage)")
            page.locator("#option-new_password").fill("short")
            with page.expect_response(
                lambda response: (
                    response.url.endswith("/api/jobs") and response.request.method == "POST"
                )
            ) as rejected:
                page.locator("#run-button").click()
            assert rejected.value.status == 400
            expect(page.locator("#option-new_password")).to_have_value("")
            expect(page.locator("#run-button")).to_be_enabled()
            assert source.read_bytes() == original and not errors
        finally:
            browser.close()


@NATIVE
@BROWSER
def test_browser_docx_upload_reaches_native_conversion(http, tmp_path):
    from playwright.sync_api import expect, sync_playwright

    source = tmp_path / "public-office.docx"
    source.write_bytes(minimal_docx())
    original = source.read_bytes()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=shutil.which("chromium"), args=["--no-sandbox"]
        )
        try:
            page = browser.new_page(accept_downloads=True)
            page.goto(f"{http.base}/#token={http.app.token}")
            accepted = page.locator("#file-input").get_attribute("accept")
            assert all(extension in accepted for extension in (".docx", ".xlsx", ".pptx"))
            page.locator("#file-input").set_input_files(source)
            expect(page.locator("#file-count")).to_have_text("1")
            page.select_option("#operation", "oxide_office_import")
            with page.expect_response(
                lambda response: (
                    response.url.endswith("/api/jobs") and response.request.method == "POST"
                )
            ) as event:
                page.locator("#run-button").click()
            assert event.value.status == 202
            expect(page.locator("#run-button")).to_be_enabled(timeout=20000)
            job = http.app.jobs[event.value.json()["id"]]
            assert job.state == "completed", job.snapshot()
            output = next(item for item in job.results if item["name"].endswith(".pdf"))
            with page.expect_download() as download:
                page.get_by_role("button", name=f"Download {output['name']}", exact=True).click()
            text = pdf_text(Path(download.value.path()).read_bytes())
            assert "Original0120.15-2.00" in "".join(text.split())
            assert source.read_bytes() == original
        finally:
            browser.close()
