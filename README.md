# PDF Multitool

A local Python PDF workbench with a browser UI and scriptable CLI. Processing runs
on the machine hosting Python. This is a development prototype, not an
internet-hosted service or browser-local WASM build.

## Draft import status

This imports the tested Python reference source into the repository's existing
`src/` layout. The required CI and dependency-lock integration are not yet complete.
See [source and runtime provenance](docs/provenance.md) before installing or running.
The existing repository requires Python >=3.14; historical engine regression
coverage used Python 3.12 and does not prove 3.14 compatibility.

## Capabilities

- Batch PDF merge, split, extract and rotate
- Bounded JPEG, PNG and JPEG2000 analysis and image-to-PDF conversion
- PDF page, text, font, form and signature-field inspection
- Poppler rendering/text extraction and explicitly acknowledged Ghostscript rewrite
- HTML, Markdown and structured text export
- ZIP/TAR/gzip intake and ZIP/TAR.GZ result packaging
- Password authentication, AES-256 protection and unlocking
- Form inspection, supported field creation/filling and flattening
- Native text/rectangle redaction with post-save checks
- Local multi-file browser interface, job cancellation and result downloads

Signature-field inspection does not verify or create cryptographic signatures.
OCR/Docling and WeasyPrint integrations need separately verified engines and are
not included in real-engine acceptance. Read the limits for
[forms](docs/FORMS.md), [images](docs/IMAGES.md),
[archives and exports](docs/ARCHIVES_AND_EXPORTS.md), and
[local security](docs/SECURITY.md).

## Running after the dependency gate is resolved

The CLI entry point is `pdf-multitool`; `python -m pdf_multitool` is equivalent
inside a correctly provisioned environment. Examples:

```sh
uv run pdf-multitool doctor
uv run pdf-multitool gui
uv run pdf-multitool merge first.pdf second.pdf -o output
uv run pdf-multitool split document.pdf --pages '1-3;4;5-7'
```

Open the printed loopback URL on the same machine. The server is single-user;
never expose it through a public tunnel or reverse proxy. Download temporary GUI
results before closing the server. Originals on disk are not overwritten.

## Repository checks and release policy

Run these before opening a merge-ready PR. CI runs the same checks:

```sh
uv run ruff format && uv run ruff check && uv run pytest && uv audit --preview-features audit-command
npx @stoplight/spectral-cli lint openapi.yaml
shellcheck scripts/*.sh
npm run typecheck && npm run test:ts
```

Agents must use a branch and PR with passing required `ci`, including when
authenticated as the owner. Main is protected. Every merge to main automatically
releases under the existing workflows; this draft must not be merged until all
integration and release gates have been reviewed. Versions come from git tags.
