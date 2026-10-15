# Toolkit scope and qualification

This is an implementation/qualification ledger, not a declaration that every
upstream toolkit API is a supported application feature. Unsupported and
unqualified work remains part of the requested scope. No private source has been
imported. Existing source and frozen experiments remain available in the base
checkpoint and preservation bundle.

The readable official-source inventory was received after the earlier checkpoint:
198 feature families and 464 public references. Its 1,626 proposed initial cases
are not executed tests, and the eventual public-symbol denominator remains unknown.
The [machine-readable reconciliation](feature_reconciliation.json) records every
family's actual route, source/tests and remaining contract. No family is declared
fully qualified. The [SVG follow-up](SVG_QUALIFICATION_20261015.md) adds a bounded
PY-006 route and records the matrix hashes and version differences.
The [recoloring follow-up](RECOLOR_QUALIFICATION_20261015.md) adds whole-document
PY-036 gray/RGB/CMYK conversion; exact colorimetry and print proofing remain unqualified.

| Toolkit and chosen version | Application coverage | Remaining boundaries | Official source |
| --- | --- | --- | --- |
| PyMuPDF 1.26.7 / bundled MuPDF | Page merge/split/rotation; rendering; native extraction; redaction; forms; encryption; creation, headers/footers, object deduplication and bounded SVG page export | General layout reconstruction, full annotation/bookmark editing, every document format, cryptographic signatures and semantic completeness are not implemented. The supplied inventory references 1.28.2; this pinned version is not silently relabelled. | [PyMuPDF documentation](https://pymupdf.readthedocs.io/en/latest/) |
| WeasyPrint 70.0 | Inline HTML/CSS generation and reflow through existing guarded fetchers; system font and paged layout integration | All uploaded resource URLs remain blocked. Full CSS feature fidelity, PDF/A/PDF/UA conformance, font/resource bundling and arbitrary web-page fetching are not qualified. | [WeasyPrint API](https://doc.courtbouillon.org/weasyprint/stable/api_reference.html) |
| pdfplumber 0.11.10 | Character/word geometry, text, explicit duplicate-character suppression; ruled and aligned-text table candidates, JSON/CSV | No universal borderless-table/prose discriminator, OCR or inferred correctness percentage. Existing frozen ambiguity evidence remains unchanged. | [Official repository](https://github.com/jsvine/pdfplumber) |
| pikepdf 10.17.0 / qpdf 12.4.2 | Syntax inspection, object streams, configurable lossless Flate recompression/linearization, attachment extraction and inventory | No rasterization. Upstream Canvas/forms APIs are not a claim that this app exposes all of them; current form workflows use PyMuPDF. No general content editing or signature verification. | [Main API](https://pikepdf.readthedocs.io/en/latest/api/main.html), [attachments](https://pikepdf.readthedocs.io/en/latest/topics/attachments.html) |
| Ghostscript, installed version recorded per run | Four compression profiles with explicit rewrite acknowledgment; bounded PNG rendering | Native rewrites can lose nonvisual data. Arbitrary PostScript/program execution, all output devices, color-management/proofing and standards conformance are not exposed/qualified. The unrelated PyPI `ghostprint` terminal-animation package is not installed. | [High-level devices](https://ghostscript.readthedocs.io/en/latest/VectorDevices.html) |
| Tesseract, installed version recorded per run | System-language OCR to searchable image PDF, TXT and TSV, language/PSM/DPI options, source-page report | Only installed language data; model files' upstream authenticity is not independently attested by the app. No accuracy claim beyond explicit synthetic assertions. No handwriting or layout guarantee. | [Command-line outputs](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html) |
| Docling[easyocr] 2.138.0 | Existing offline configuration, verified-manifest gate and PDF structured conversion adapter; non-success conversion now fails explicitly | Full inference remains unqualified without a complete independently verified model set. Audio/video ASR, office formats and all OCR backends are not integrated; CUDA/Python 3.12-only extras are not assumed compatible with 3.14. Transcription is not codec muxing. | [Official documentation](https://docling-project.github.io/docling/) |
| Poppler system tools; requested 26.10 comparison | Existing guarded pdftotext/pdftoppm workflows, installed version reported | This host reports 26.05.0. Official 26.10 source download returned HTTP 403. No 26.10 source build, release comparison, signing or every utility's capabilities are claimed. | [Official release page](https://poppler.freedesktop.org/) |

The new dependencies are exactly pinned and locked; the original PyMuPDF,
WeasyPrint and Docling pins remain unchanged. PyPI exposed a CPython 3.14 Linux
abi3 wheel for pikepdf 10.17.0 and a universal wheel for pdfplumber 0.11.10. Local
integration tests exercise these installed versions on Python 3.14.8. This is
not certification of every Python build, architecture or operating system.

## Cross-cutting requested features

| Feature | Status at this checkpoint |
| --- | --- |
| Creation, pagination, headers/footers | Implemented for plain Unicode text with measured wrapping and missing-glyph rejection; HTML/CSS path retained. Rotated header/footer targets fail explicitly. |
| Cleanup and deduplication | Structural cleanup, exact object/stream dedup and configurable lossless compression are implemented. Duplicate-character extraction is opt-in. Whole-page/content-semantic dedup is not implemented. |
| Deskew | Bounded planar angle correction, automatic/manual, with per-page evidence; uncertain detections are partial. Rasterization acknowledgment required. |
| Curved-page dewarp | Unsupported: no qualified curvature model or correction implementation. Deskew is not called dewarp. |
| Variable compression | Existing Ghostscript profiles plus pikepdf Flate levels 1–9; no guaranteed size reduction. Fine-grained JPEG/color sampling controls are not exposed. |
| Parsing/extraction | Native text, geometry and candidate-table routes implemented; source labels/reports preserved. OCR and inferred table/text order still require review. |
| Rasterization | Poppler, MuPDF and Ghostscript paths, explicit DPI and output bounds; requested Poppler 26.10 comparison remains blocked. |
| Recoloring | Whole-document gray/RGB/CMYK component conversion with explicit acknowledgment and reopened text/geometry checks. No selected-page editing, calibrated proofing, custom ICC profiles or spot/overprint fidelity claim. |
| OCR | Tesseract real synthetic inference tested. Docling model inference remains gated. Image input can first use Images → PDF; direct image-to-OCR job intake is not yet supported. |
| Forms | Existing inspection/filling/creation/flattening and failure tests retained. No cryptographic signing, inferred form answers or JavaScript/XFA execution. |
| Media/audio | Safe document-attachment extraction and media-annotation inventory only. No playback, native codec conversion, general muxing or qualified transcription. |
| Apple Vision / Neural Engine | Unsupported on this Linux host; no user's Mac accessed. Requires a separate Apple-platform qualification effort. |
| GUI/API | New tools share explicit contracts, actual backend dispatch and live progress/cancel/recovery/download flows. General canvas editing, arbitrary toolkit switches and universal accessibility compliance are not claimed. |
| Process/network limits | Kernel syscall filter covers native helpers on Linux; bounded extra UID task headroom, memory/CPU/output/descriptor limits, timeouts and group cleanup. Other-UID/privileged/cgroup guarantees are not claimed. |
| Filesystem sandbox | Actual host limitation: Bubblewrap user-namespace setup denied; Landlock syscall returned ENOSYS. Native parsers still need external filesystem isolation for hostile documents. |

Full completion against the user's entire named inventory is **not established**.
No acceptance oracle or old experiment was rewritten to make this ledger green.
