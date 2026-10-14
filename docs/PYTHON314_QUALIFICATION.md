# Python 3.14 qualification

Work starts from PR #3 (`8fd657da67d57c9107b42cbbdabf1bfab0ae1dfc`).
The first `uv sync --locked --dry-run` reproduced the stale-lock failure.
A normal `uv lock` resolves the declared core and optional dependencies; neither
`--frozen` nor dependency omission is used. Core engine pins remain PyMuPDF
1.26.6, Pillow 12.3.0, WeasyPrint 70.0 and Docling 2.138.0.

The development group now includes Pillow (image tests import it) and ReportLab
(synthetic form fixtures import it). The lock selects ReportLab 4.5.1. These are
test dependencies, not additional mandatory application engines. The configured
index is official PyPI. Optional OCR dependencies are resolved but have not been
installed or qualified; resolution alone does not prove runtime compatibility.

## Local environment and provenance

The local interpreter is uv-managed CPython 3.14.7, Linux x86-64. This uv's
managed download catalog could not provide 3.14.8. A passing local result must
not be reported as a test of CI's 3.14.8 interpreter.

Before installing the selected core, image, HTML and development wheels, the
repository verifier compared the exact filename, HTTPS URL, size and SHA-256 in
PyPI's release JSON and Simple JSON disclosures. Downloaded wheel bytes then
matched the declared size and SHA-256. All selected transitive wheels received
the same check. This is agreement from one publisher authority, not independent
trust roots, a security audit or a verified Docling model set. Raw metadata and
wheel files are local verification artifacts, excluded from the source commit.

The selected PyMuPDF artifact is
`pymupdf-1.26.6-cp310-abi3-manylinux_2_28_x86_64.whl`, SHA-256
`7777d0b7124c2ebc94849536b6a1fb85d158df3b9d873935e63036559391534c`.
The selected Pillow artifact is
`pillow-12.3.0-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl`, SHA-256
`251bf95b67017e27b13d82f5b326234ca62d70f9cf4c2b9032de2358a3b12c7b`.

Poppler, Ghostscript and Pandoc are preinstalled system tools. Their upstream
artifact provenance has not been independently verified in this qualification.
No model downloads are enabled. No release or main-branch merge is authorized
by these results.

## Validation checkpoint

- Ruff formatting and lint pass with the existing rule selection.
- The first pytest run failed collection because ReportLab was undeclared.
  With the corrected environment: 248 tests and 335 subtests pass on CPython
  3.14.7, including real native-engine and loopback HTTP tests (52.95 seconds).
- Artifact verifier: 13 tests pass; six mocked UI/API scenarios pass.
- TypeScript typecheck and one runtime test pass; OpenAPI Spectral lint passes.
- Wheel and sdist build pass. Installed-artifact smoke remains to be completed.
- Local dependency audit cannot contact `api.osv.dev` (tunnel connection error).
  This is not a clean audit. Shellcheck is absent locally.
- Hosted required checks remain pending. Real-browser/Safari QA and real Docling
  conversion remain unverified.
