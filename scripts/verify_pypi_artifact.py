#!/usr/bin/env python3
"""Check two official PyPI SHA-256 disclosures before any artifact download.

This deliberately does not resolve dependencies, build, install, import, or run a
downloaded package. Pin and verify every transitive dependency separately.
PyPI JSON and Simple are two disclosures by one authority, not independent trust
roots. See docs/provenance.md for the limits of this check.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path


class VerificationError(Exception):
    pass


def canonical_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise VerificationError("Invalid package name")
    return re.sub(r"[-_.]+", "-", name).lower()


def require_url(url: str, hosts: set[str]) -> str:
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in hosts
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise VerificationError(f"Refusing a non-official or non-HTTPS URL: {url}")
    return urllib.parse.urlunsplit(parts._replace(fragment=""))


class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_host = urllib.parse.urlsplit(req.full_url).hostname
        require_url(newurl, {old_host})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def read_json(url: str, *, simple: bool = False) -> dict:
    require_url(url, {"pypi.org"})
    headers = {
        "User-Agent": "pdf-multitool-provenance/1.0",
        "Accept": "application/vnd.pypi.simple.v1+json" if simple else "application/json",
    }
    opener = urllib.request.build_opener(OfficialRedirects())
    with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as resp:  # noqa: S310 -- HTTPS host and redirects validated
        require_url(resp.geturl(), {"pypi.org"})
        body = resp.read(32 * 1024 * 1024 + 1)
        if len(body) > 32 * 1024 * 1024:
            raise VerificationError("Metadata exceeds the 32 MiB safety limit")
        return json.loads(body)


def compare_metadata(
    project: str, version: str, filename: str, release: dict, simple: dict
) -> dict:
    project = canonical_name(project)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version):
        raise VerificationError("An exact version is required")
    if Path(filename).name != filename or not filename.endswith(".whl"):
        raise VerificationError("Use one exact wheel filename; source builds are disabled")
    if canonical_name(release.get("info", {}).get("name", "")) != project:
        raise VerificationError("Release metadata project mismatch")
    if release["info"].get("version") != version:
        raise VerificationError("Release metadata version mismatch")
    if canonical_name(simple.get("name", "")) != project:
        raise VerificationError("Simple metadata project mismatch")
    release_files = [f for f in release.get("urls", []) if f.get("filename") == filename]
    simple_files = [f for f in simple.get("files", []) if f.get("filename") == filename]
    if len(release_files) != 1 or len(simple_files) != 1:
        raise VerificationError("Exact filename must occur once in each official disclosure")
    a, b = release_files[0], simple_files[0]
    if a.get("yanked") or b.get("yanked"):
        raise VerificationError("Refusing a yanked file")
    sha_a = a.get("digests", {}).get("sha256", "")
    sha_b = b.get("hashes", {}).get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", sha_a) or sha_a != sha_b:
        raise VerificationError("Missing or mismatched upstream SHA-256; artifact not requested")
    url_a = require_url(a.get("url", ""), {"files.pythonhosted.org"})
    url_b = require_url(b.get("url", ""), {"files.pythonhosted.org"})
    if url_a != url_b:
        raise VerificationError("The disclosures identify different artifact URLs")
    if urllib.parse.unquote(urllib.parse.urlsplit(url_a).path.rsplit("/", 1)[-1]) != filename:
        raise VerificationError("URL basename does not match the exact filename")
    size = a.get("size")
    if not isinstance(size, int) or size <= 0:
        raise VerificationError("Release metadata lacks a valid artifact byte count")
    if b.get("size") is not None and b["size"] != size:
        raise VerificationError("Published sizes disagree")
    return {
        "schema_version": 1,
        "project": project,
        "version": version,
        "filename": filename,
        "download_url": url_a,
        "expected_size": size,
        "sha256": sha_a,
        "release_json_sha256": sha_a,
        "simple_json_sha256": sha_b,
        "release_json_url": f"https://pypi.org/pypi/{project}/{version}/json",
        "simple_json_url": f"https://pypi.org/simple/{project}/",
        "metadata_matched_at": utc_now(),
        "artifact_downloaded": False,
        "publisher_upload_time": a.get("upload_time_iso_8601"),
        "license_expression": release["info"].get("license_expression"),
        "license": release["info"].get("license"),
        "requires_python": a.get("requires_python"),
        "requires_dist": release["info"].get("requires_dist"),
        "project_urls": release["info"].get("project_urls"),
        "assurance": (
            "Matching official PyPI JSON and Simple disclosures; same "
            "authority, not independent trust roots"
        ),
    }


def download(record: dict, directory: Path, max_bytes: int) -> Path:
    if record["expected_size"] > max_bytes:
        raise VerificationError("Artifact exceeds configured size cap; no download attempted")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / record["filename"]
    if target.exists():
        raise VerificationError(f"Refusing to overwrite {target}; preserve the existing artifact")
    partial = directory / (record["filename"] + ".partial")
    if partial.exists():
        raise VerificationError(f"Refusing to overwrite {partial}")
    opener = urllib.request.build_opener(OfficialRedirects())
    digest = hashlib.sha256()
    size = 0
    try:
        with opener.open(record["download_url"], timeout=60) as resp, partial.open("xb") as out:
            require_url(resp.geturl(), {"files.pythonhosted.org"})
            while chunk := resp.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes or size > record["expected_size"]:
                    raise VerificationError("Response exceeds the approved size")
                digest.update(chunk)
                out.write(chunk)
        if size != record["expected_size"] or digest.hexdigest() != record["sha256"]:
            raise VerificationError("Downloaded bytes fail size/SHA-256 verification")
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    record.update(
        artifact_downloaded=True,
        verified_at=utc_now(),
        actual_size=size,
        actual_sha256=digest.hexdigest(),
        local_path=str(target.resolve()),
    )
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("version")
    parser.add_argument("filename")
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download only after both official SHA-256 values match",
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1] / ".verified"
    )
    parser.add_argument("--max-mib", type=int, default=100)
    args = parser.parse_args()
    project = canonical_name(args.project)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", args.version):
        raise VerificationError("Pin an exact release version")
    release = read_json(f"https://pypi.org/pypi/{project}/{args.version}/json")
    simple = read_json(f"https://pypi.org/simple/{project}/", simple=True)
    record = compare_metadata(project, args.version, args.filename, release, simple)
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    evidence = args.root / "metadata" / f"{project}-{args.version}-{timestamp}"
    evidence.mkdir(parents=True, exist_ok=False)
    (evidence / "release.json").write_text(json.dumps(release, indent=2) + "\n")
    (evidence / "simple.json").write_text(json.dumps(simple, indent=2) + "\n")
    result_path = evidence / "verification.json"
    result_path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"Official metadata matched BEFORE download: {record['sha256']}", flush=True)
    if args.download:
        path = download(record, args.root / "wheelhouse", args.max_mib * 1024 * 1024)
        result_path.write_text(json.dumps(record, indent=2) + "\n")
        print(f"Artifact bytes verified: {path}")
    print(f"Evidence: {result_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (VerificationError, OSError, ValueError) as exc:
        print(f"Verification stopped: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
