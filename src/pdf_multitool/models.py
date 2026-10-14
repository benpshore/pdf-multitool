"""Fail-closed model provenance gate; never fetches models at runtime."""

import hashlib
import json
from pathlib import Path

from .core import JobError


def validate_model_manifest(manifest_path: str | None) -> Path:
    if not manifest_path:
        raise JobError(
            "Docling needs an explicit verified model manifest. No models are "
            "downloaded automatically."
        )
    manifest = Path(manifest_path).resolve(strict=True)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    root = (manifest.parent / data["artifacts_path"]).resolve(strict=True)
    if not root.is_dir() or not data.get("files"):
        raise JobError("Model manifest must list every model file")
    expected_paths = set()
    for item in data["files"]:
        file = (root / item["path"]).resolve(strict=True)
        if not file.is_relative_to(root) or not file.is_file():
            raise JobError("Model manifest escapes its artifact directory")
        expected_paths.add(file)
        digest = item["sha256"]
        # Two matching pre-download publications must be recorded by the setup verifier.
        if (
            digest != item.get("upstream_sha256")
            or not item.get("source_url", "").startswith("https://")
            or not item.get("checksum_url", "").startswith("https://")
        ):
            raise JobError("Model manifest lacks matching authoritative SHA-256 evidence")
        if len(digest) != 64:
            raise JobError("Invalid model SHA-256")
        with file.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise JobError(f"Model integrity check failed: {file.name}")
    actual_paths = {p.resolve() for p in root.rglob("*") if p.is_file()}
    if actual_paths != expected_paths:
        raise JobError("Unlisted model files found. Verify the complete model directory first.")
    return root
