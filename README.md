# PDF Multitool

A local Python PDF workbench with a browser UI and scriptable CLI. Processing runs
on the machine hosting Python. This is a development prototype, not an
internet-hosted service or browser-local WASM build.

## Draft import status

This imports the Python reference source into the repository's `src/` layout.
The declared dependencies now have a resolved lock. Python 3.14 qualification,
artifact provenance, check results and remaining limits are recorded in
[the qualification report](docs/PYTHON314_QUALIFICATION.md) and
[source and runtime provenance](docs/provenance.md). This remains a draft;
passing synthetic checks does not establish full product acceptance.

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
- Unicode text PDF creation, page headers/footers and exact object deduplication
- pdfplumber text/geometry and candidate-table exports; pikepdf stream optimization
- Embedded attachment inventory/extraction, Tesseract OCR and bounded planar deskew
- Additional MuPDF/Ghostscript rendering and shared API/GUI option contracts
- SVG page export with selectable glyph paths or font-dependent text
- Whole-document gray, RGB or CMYK conversion with text/geometry verification

For a known row-based record layout, the `pdfhtml`, `markdown` and `textlayout`
commands accept `--reading-order rows`; `columns` reads down separated columns.
The GUI exposes the same choice. The default `auto` policy is unchanged and can
export borderless tables column by column. Explicit ordering applies to the whole
document, requires unrotated horizontal left-to-right content, and does not infer
table cell identities or guarantee mixed-layout correctness. See
[reading-order qualification](docs/READING_ORDER_QUALIFICATION_20261015.md).

Signature-field inspection does not verify or create cryptographic signatures.
Docling OCR remains unavailable without a separately verified model set. Tesseract
OCR uses separately installed system language data and is available through its
own explicitly acknowledged rasterization workflow.
Optional WeasyPrint conversion has synthetic Linux integration coverage with
resource loading disabled; it does not promise layout fidelity. Read the limits for
[forms](docs/FORMS.md), [images](docs/IMAGES.md),
[archives and exports](docs/ARCHIVES_AND_EXPORTS.md), and
[local security](docs/SECURITY.md).
The [toolkit feature matrix](docs/TOOLKIT_FEATURE_MATRIX.md) records implemented,
partial, unsupported and unqualified scope. The [API reference](docs/API.md)
describes the actual loopback routes and operation options.
The [198-family reconciliation](docs/feature_reconciliation.json) maps the supplied
inventory to product routes without claiming complete public-API coverage.
The [SVG qualification](docs/SVG_QUALIFICATION_20261015.md) records its bounds,
independent rendering checks and outstanding acceptance work.
The [recoloring qualification](docs/RECOLOR_QUALIFICATION_20261015.md) records
component conversion, explicit color-loss acknowledgment and remaining color-fidelity limits.

## Running locally

The CLI entry point is `pdf-multitool`; `python -m pdf_multitool` is equivalent
inside a correctly provisioned Python >=3.14 environment. Install the locked
environment, then inspect engine availability:

```sh
uv sync --locked
uv run --locked pdf-multitool doctor
uv run --locked pdf-multitool gui
uv run --locked pdf-multitool merge first.pdf second.pdf -o output
uv run --locked pdf-multitool split document.pdf --pages '1-3;4;5-7'
uv run --locked pdf-multitool svg document.pdf --pages '1-3' --text-mode paths --scale 1
uv run --locked pdf-multitool recolor document.pdf --colorspace gray --allow-color-change
```

Poppler's `pdftotext` and `pdftoppm` provide text extraction and rendering;
Ghostscript provides the explicitly acknowledged compression operation. These
are separate system tools. Image support uses the `images` extra. HTML-to-PDF
and repagination use `uv sync --locked --extra html` and system Pango libraries.
CI provisions these dependencies on Ubuntu; other platforms remain unqualified.
Installing the `ocr` extra alone does not enable model downloads or satisfy the
Docling model verification gate.

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
