"""Fail-closed model provenance gate; never fetches models at runtime."""

import hashlib
import re
from pathlib import Path

from .core import JobError, read_local_json


def validate_model_manifest(manifest_path: str | None) -> Path:
    if not manifest_path:
        raise JobError(
            "Docling needs an explicit verified model manifest. No models are "
            "downloaded automatically."
        )
    manifest = Path(manifest_path).resolve(strict=True)
    # A malformed operator-supplied manifest must disable Docling, not crash
    # workbench startup before the other supported engines can be used.
    # Resolve the configured alias as before, then reject FIFOs/devices and
    # last-component symlink swaps with the shared nonblocking regular-file reader.
    data = read_local_json(manifest, maximum=1024 * 1024)
    if not isinstance(data, dict):
        raise JobError("Model manifest must be a JSON object")
    if not isinstance(data.get("artifacts_path"), str) or not data["artifacts_path"]:
        raise JobError("Model manifest needs an artifacts_path string")
    if not isinstance(data.get("files"), list) or not data["files"]:
        raise JobError("Model manifest must list every model file")
    root = (manifest.parent / data["artifacts_path"]).resolve(strict=True)
    if not root.is_dir() or not data.get("files"):
        raise JobError("Model manifest must list every model file")
    expected_paths = set()
    for item in data["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
            raise JobError("Model manifest entries need a file path string")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise JobError("Invalid model SHA-256")
        file = (root / item["path"]).resolve(strict=True)
        if not file.is_relative_to(root) or not file.is_file():
            raise JobError("Model manifest escapes its artifact directory")
        expected_paths.add(file)
        # Two matching pre-download publications must be recorded by the setup verifier.
        if (
            digest != item.get("upstream_sha256")
            or not isinstance(item.get("source_url"), str)
            or not item["source_url"].startswith("https://")
            or not isinstance(item.get("checksum_url"), str)
            or not item["checksum_url"].startswith("https://")
        ):
            raise JobError("Model manifest lacks matching authoritative SHA-256 evidence")
        with file.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise JobError(f"Model integrity check failed: {file.name}")
    actual_paths = {p.resolve() for p in root.rglob("*") if p.is_file()}
    if actual_paths != expected_paths:
        raise JobError("Unlisted model files found. Verify the complete model directory first.")
    return root
