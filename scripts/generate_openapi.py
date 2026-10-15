"""Generate the real HTTP contract. JSON is a valid YAML 1.2 document."""

import json
from pathlib import Path

from pdf_multitool.catalog import OPERATIONS

LEGACY = [
    "merge",
    "imagepdf",
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
    "clean",
    "repaginate",
    "forms",
    "signatures",
    "redact",
    "unlock",
    "protect",
    "markdown",
    "textlayout",
    "unpack",
    "bundle",
    "preview",
]


def reference(name):
    return {"$ref": f"#/components/schemas/{name}"}


def response(description, schema=None, media="application/json"):
    return {
        "description": description,
        **({"content": {media: {"schema": schema}}} if schema else {}),
    }


def endpoint(
    identifier, summary, status="200", schema=None, description=None, media="application/json"
):
    return {
        "operationId": identifier,
        "summary": summary,
        "description": description or summary,
        "tags": ["workbench"],
        "responses": {
            status: response(summary, schema, media),
            "400": response(
                "Invalid request, exhausted quota, unsupported option or busy workspace",
                reference("Error"),
            ),
            "401": response("Missing or invalid X-PDF-Token", reference("Error")),
            "403": response("Host or Origin is not this loopback server", reference("Error")),
            "404": response("Unknown file, job or result", reference("Error")),
        },
    }


def body(schema, media="application/json"):
    return {"required": True, "content": {media: {"schema": schema}}}


def obj(properties, required=(), **kw):
    return {
        "type": "object",
        "properties": properties,
        **({"required": list(required)} if required else {}),
        **kw,
    }


def build():
    string = {"type": "string"}
    binary = {"type": "string", "format": "binary"}
    files = {"type": "array", "items": string, "minItems": 1, "maxItems": 200, "uniqueItems": True}
    schemas = {
        "Error": obj({"error": string}, ["error"]),
        "Id": obj({"id": string}, ["id"]),
        "File": obj(
            {
                "id": string,
                "name": string,
                "size": {"type": "integer", "minimum": 0},
                "member_path": string,
                "source_archive": {"type": "object"},
            },
            ["id", "name", "size"],
        ),
        "Result": obj(
            {
                "id": string,
                "name": string,
                "size": {"type": "integer", "minimum": 0},
                "url": string,
                "bundle_path": string,
                "text": string,
                "source_member_path": string,
            },
            ["id", "name", "size", "url"],
        ),
        "Job": obj(
            {
                "id": string,
                "operation": string,
                "state": {
                    "type": "string",
                    "enum": ["queued", "running", "completed", "partial", "failed", "cancelled"],
                },
                "progress": {"type": "number", "minimum": 0, "maximum": 1},
                "message": string,
                "results": {"type": "array", "items": reference("Result")},
                "errors": {
                    "type": "array",
                    "items": obj({"name": string, "error": string}, ["name", "error"]),
                },
                "warnings": {"type": "array", "items": string},
            },
            ["id", "operation", "state", "progress", "message", "results", "errors", "warnings"],
        ),
    }
    requests = [
        obj(
            {
                "operation": {"type": "string", "enum": LEGACY},
                "files": files,
                "options": {
                    "type": "object",
                    "description": "Existing options are listed in docs/API.md.",
                },
            },
            ["operation", "files"],
        )
    ]
    for name, contract in OPERATIONS.items():
        properties = {}
        for key, spec in contract["options"].items():
            properties[key] = {k: v for k, v in spec.items() if k not in {"label", "multiline"}}
            properties[key]["description"] = spec["label"]
        if contract["inputs"]:
            properties["password"] = {"type": "string", "maxLength": 127, "writeOnly": True}
        if contract["pages"]:
            properties["pages"] = {
                "type": "string",
                "maxLength": 4096,
                "description": "One-based pages, e.g. 1,3-5; empty selects all.",
            }
        schemas[name + "Options"] = obj(properties, additionalProperties=False)
        requests.append(
            obj(
                {
                    "operation": {"const": name},
                    "files": files
                    if contract["inputs"]
                    else {"type": "array", "items": string, "maxItems": 0},
                    "options": reference(name + "Options"),
                },
                ["operation", "files"],
                description=contract["description"],
            )
        )
    schemas["JobRequest"] = {"oneOf": requests}
    paths = {
        "/api/status": {
            "get": endpoint(
                "getStatus",
                "Engine versions, limits and feature contracts",
                schema=obj(
                    {
                        "engines": {"type": "object"},
                        "limits": {"type": "object"},
                        "capabilities": {"type": "object"},
                        "mode": string,
                        "storage": string,
                    },
                    ["engines", "limits", "capabilities", "mode", "storage"],
                ),
            )
        },
        "/api/files": {
            "get": endpoint(
                "listFiles",
                "List uploaded files",
                schema=obj({"files": {"type": "array", "items": reference("File")}}, ["files"]),
            ),
            "post": {
                **endpoint(
                    "uploadFile",
                    "Upload original bytes or atomically expand an archive",
                    "201",
                    {
                        "oneOf": [
                            reference("File"),
                            obj(
                                {
                                    "files": {"type": "array", "items": reference("File")},
                                    "warnings": {"type": "array", "items": string},
                                    "statistics": {"type": "object"},
                                },
                                ["files"],
                            ),
                        ]
                    },
                    (
                        "Send raw bytes with Content-Length and a URL-encoded "
                        "X-Filename. PDF, HTML, JPEG, PNG, JPEG2000, ZIP/TAR/gzip "
                        "only. Quotas are reserved before streaming; no nested "
                        "archive expansion."
                    ),
                ),
                "parameters": [
                    {
                        "name": "X-Filename",
                        "in": "header",
                        "schema": string,
                        "description": "URL-encoded display name, not a filesystem path.",
                    }
                ],
                "requestBody": body(binary, "application/octet-stream"),
            },
        },
        "/api/files/{file_id}": {
            "delete": endpoint(
                "deleteFile",
                "Remove an idle uploaded file",
                schema=obj({"ok": {"type": "boolean"}}),
            )
        },
        "/api/files/{file_id}/original": {
            "get": endpoint(
                "downloadOriginal",
                "Download preserved original bytes",
                schema=binary,
                media="application/octet-stream",
            )
        },
        "/api/files/{file_id}/preview": {
            "get": endpoint(
                "previewFile",
                "Render a bounded first-page preview",
                schema=binary,
                media="image/png",
            )
        },
        "/api/archives/{file_id}/original": {
            "get": endpoint(
                "downloadArchive",
                "Download preserved original archive",
                schema=binary,
                media="application/octet-stream",
            )
        },
        "/api/jobs": {
            "post": {
                **endpoint(
                    "startJob",
                    "Start one bounded asynchronous job",
                    "202",
                    reference("Id"),
                    (
                        "Only one job may be queued/running. A batch may finish "
                        "partial. All outputs from each input are published together "
                        "after validation; failures discard that part. Poll job "
                        "state; HTTP 202 is not completion."
                    ),
                ),
                "requestBody": body(reference("JobRequest")),
            }
        },
        "/api/jobs/{job_id}": {
            "get": endpoint(
                "getJob", "Read progress, status, warnings and results", schema=reference("Job")
            )
        },
        "/api/jobs/{job_id}/cancel": {
            "post": {
                **endpoint(
                    "cancelJob",
                    "Request cancellation and process-group cleanup",
                    schema=reference("Job"),
                    description=(
                        "Cancellation is asynchronous and idempotent. Poll for a "
                        "terminal state. Completed parts remain downloadable; "
                        "unfinished parts are discarded."
                    ),
                ),
                "requestBody": body(obj({})),
            }
        },
        "/api/results/{job_id}/{result_id}": {
            "get": endpoint(
                "downloadResult",
                "Download a validated published result",
                schema=binary,
                media="application/octet-stream",
            )
        },
        "/api/bundles": {
            "post": {
                **endpoint(
                    "bundleResults",
                    "Package completed outputs, preserving companion paths",
                    "202",
                    reference("Id"),
                ),
                "requestBody": body(
                    obj(
                        {"job": string, "format": {"type": "string", "enum": ["zip", "tar.gz"]}},
                        ["job", "format"],
                    )
                ),
            }
        },
    }
    for path, item in paths.items():
        params = [name for name in ("file_id", "job_id", "result_id") if "{" + name + "}" in path]
        if params:
            item["parameters"] = [
                {
                    "in": "path",
                    "name": name,
                    "required": True,
                    "schema": string,
                    "description": "Opaque server-issued identifier.",
                }
                for name in params
            ]
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "PDF Workbench API",
            "version": "0.1.0",
            "description": (
                "Single-user loopback API; generated by scripts/generate_openapi.py. "
                "Original bytes are retained. No internet-hosting or universal "
                "document-fidelity guarantee."
            ),
            "contact": {"url": "https://github.com/benpshore/pdf-multitool"},
        },
        "servers": [{"url": "http://127.0.0.1:{port}", "variables": {"port": {"default": "8000"}}}],
        "tags": [
            {"name": "workbench", "description": "Local document jobs and private session storage"}
        ],
        "security": [{"LocalToken": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "LocalToken": {"type": "apiKey", "in": "header", "name": "X-PDF-Token"}
            },
            "schemas": schemas,
        },
    }


if __name__ == "__main__":
    Path("openapi.yaml").write_text(json.dumps(build(), indent=2) + "\n")
