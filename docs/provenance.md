# Source and runtime provenance

This draft imports the Python reference implementation from development snapshot
`05ae9d0ba3b3689d772fb13c97fc980b1f0753c6` (prototype milestone 0.4.2).
That identifier records source provenance, not a public release or supply-chain
attestation. Historical local development evidence recorded 247 native-engine
tests passing. Source files are mapped from `pdf_multitool/` to
`src/pdf_multitool/`; package version reporting now follows installed metadata,
and mocked UI test paths use the new source layout.

The destination repository's dynamic tag-derived versioning, Python >=3.14
constraint, protected main branch, required CI, and release workflows are
preserved. No tag, release, deployment, or main-branch merge is part of this import.

## Pending integration gates

- The historical native tests used preinstalled Python 3.12, PyMuPDF 1.26.6,
  Pillow 12.3.0, Poppler and Ghostscript. Their upstream artifact provenance was
  not independently verified. This does not establish Python 3.14 compatibility.
- No verified engine wheelhouse, full dependency lock, or Docling model set is
  included. The destination's existing `uv.lock` is preserved but predates the
  added engine dependencies, so `uv sync --locked` must fail until an authorized,
  verified dependency-lock update is completed. Do not bypass that gate.
- WeasyPrint and Docling real-engine integration, real-browser/Safari QA, full
  product acceptance, and native/Rust/WASM ports remain unverified or deferred.
- Local repository-wide Ruff, pytest, dependency audit, and build checks require
  the destination's development toolchain. Source-only and preinstalled-engine
  regression results are separate evidence and cannot replace required CI.

The public import excludes private planning notes, internal process records,
machine inventories and generated historical review artifacts. Tests create
synthetic fixtures; no private user documents are included.
