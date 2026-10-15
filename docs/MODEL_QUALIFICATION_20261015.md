# Docling integration and recovery checkpoint — 2026-10-15

Base: `c7cea1539a0b22037d0ae76ace2f05a15954fb6a` (draft PR #5), verified
against the published branch before changes. Continuation branch:
`codex/model-qualification-20261015`.

## Model execution remains blocked

The execution environment denied the official request
`https://huggingface.co/api/models/docling-project/docling-models` with
`URLError: Tunnel connection failed: 403 Forbidden`.
No alternative artifact route was attempted, no model weights were downloaded,
and no synthetic manifest was used to authorize model inference. Real PDF model
execution and OCR accuracy remain unqualified. Browser-visible documentation is
not a substitute for executable artifact access and verified upstream hashes.

The pinned source uses layout Heron (`docling-project/docling-layout-heron`,
upstream default revision `main`) and TableFormer (`docling-project/docling-models`,
revision `v2.3.0`), plus EasyOCR assets. A future provisioner must resolve immutable
revisions and obtain matching pre-download SHA-256 evidence for the entire required
model directory. The app still requires a complete explicit manifest, checks its
local hashes, and disables automatic model downloads. Its existing Python audit
hook is not an OS network sandbox; native-library network isolation remains a gap.

## Demonstrated integration defects fixed

1. A manifest containing `{}` raised `KeyError('artifacts_path')` in `engines()`
   and could prevent workbench startup when Docling was installed. Manifest
   validation now checks object/list/string shapes and SHA-256 syntax, rejects
   duplicate JSON keys, and caps manifest input at 1 MiB. Invalid manifests disable
   Docling with a reason while other engines remain usable. Existing digest,
   evidence, containment and complete-directory checks remain enforced.
2. The app explicitly configures `EasyOcrOptions`, but `docling==2.138.0` installs
   the standard extra without EasyOCR. Official metadata and the verified wheel
   identify `docling[easyocr]==2.138.0` as the needed extra. The declaration and lock
   now include it; no model policy or runtime download permission changed.

## Artifact and runtime evidence

`docs/ocr-dependency-evidence.json` records matching official PyPI release/Simple
SHA-256 disclosures before download and matching downloaded size/digest for all
seven added packages' CPython 3.14/Linux-compatible wheels: EasyOCR 1.7.2, imageio
2.38.1, lazy-loader 0.6, ninja 1.13.2, python-bidi 0.6.11, scikit-image 0.26.0 and
tifffile 2026.9.20. Their hashes/sizes also match `uv.lock`. It includes the verified
Docling-slim 2.138.0 wheel used for source inspection. PyPI's two disclosures share
one authority; they are not independent trust roots. Remaining dependencies were
installed from the hash-locked resolution, not independently re-attested here.

```sh
uv sync --locked --extra html --extra ocr --no-build
uv run --locked --extra ocr python scripts/smoke_docling_configuration.py
```

The installed API smoke passes with EasyOCR 1.7.2 and Docling 2.138.0: construct
`EasyOcrOptions`, `PdfPipelineOptions` and `DocumentConverter` with downloads,
remote services and external plugins disabled. It uses a fresh temporary HOME,
a nonexistent artifact directory, a 6 GiB address-space limit, a 60-second CPU
limit, a 90-second parent timeout and the existing network audit hook. It does not
invoke conversion or inference. This establishes installed API compatibility,
not a successful model pipeline.

## Regression coverage

```sh
uv run --locked --extra html --extra ocr --with playwright pytest -q
```

Manifest tests cover malformed shapes and evidence fields, mutated hashes,
unlisted files, directory escape, duplicate keys and oversized manifests. A real
worker rejects the bad manifest, publishes no result and leaves the original
synthetic PDF intact; a subsequent PyMuPDF operation succeeds.

Chromium Back/reload tests now assert the same recovered job ID, exact result
endpoint, filename and downloaded bytes after each navigation. They also verify
that restoring a text result does not repeat its automatic download, a stale job
ID is cleared on a 404, and another real operation succeeds afterwards.
Combined local suite: 262 tests and 366 subtests passed in 68.76 seconds,
including Chromium and the six new manifest tests. Ruff, six DOM scenarios,
TypeScript, Spectral, ShellCheck and wheel/sdist build passed.
All documents are synthetic. Physical phones, Safari and other platforms remain
unqualified. See the PR/final handoff for exact commit and test/CI counts.

Local OSV audit remains blocked at `https://api.osv.dev/v1/querybatch` by the
network tunnel. The required hosted audit remains enabled.
