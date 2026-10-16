# PDF Oxide 0.3.78 integration — merge hold

The requested scope is the full supported PDF Oxide capability set in this
Python app. Typed, allowlisted operations now cover extraction, formatting,
creation, editing, forms, conversion, analysis and security, with explicit
gates for known defects and missing resources. Existing engines retain their contracts. No capability
is qualified merely because a Python attribute exists or a worker exits zero.

## Inventory and implementation ownership

[The installed public API inventory](pdf-oxide-public-api-0.3.78.json) records
788 entries across 76 exported types/functions, including properties, constants,
helpers, async aliases and native extension exports not re-exported at package
level. These are not 788 user-facing features. It was
generated from the pinned installed wheel under the worker's native resource
and network restrictions, without opening a document. The reproducible script
is `scripts/inventory_pdf_oxide.py`.

| Family | Module owner | Scope being implemented and independently checked |
| --- | --- | --- |
| Core extraction | Integration owner, `oxide.py` | Page text, direct table rows/cells, inert native HTML plus restricted review HTML, bounded in-memory image inspection/explicit PNG export, optional separate-instance header/footer cleanup |
| Native formatting | `oxide_format.py` | Per-page plain text, Markdown and HTML, explicit layout/heading/form controls, separate baseline; unsupported or ignored artifact controls rejected |
| Creation | `oxide_creation.py` | Text/Markdown/HTML/CSS/template constructors; fluent text/graphics/fonts/images, barcodes, bounded fields/annotations; defective native tables, radio groups and markup appearances gated |
| Editing | `oxide_editing.py` | Page selection/ranges/delete/move/merge, geometry, metadata, DOM text, image placement, attachments, outlines/labels/XMP inspection, bookmark splitting |
| Forms and annotations | `oxide_forms.py` | Field inspection/fill/export/flatten; annotations/links/highlights/notes and flattening; XFA detection and explicit unavailable mutation routes |
| Conversion and rendering | `oxide_conversion.py` | PNG/JPEG/pixmap/fit/separation rendering, image-to-PDF, PDF-to-Office and OOXML-to-PDF with archive/XML preflight |
| Security and compliance | `oxide_security.py` | Encryption/authentication/permissions, limited destructive redaction and sanitization, signature/DSS inspection and supported verification, PDF/A/UA/X native checks and actual available conversion; private-key signing remains gated |
| Analysis and OCR | `oxide_analysis.py` | Geometry/paths/characters/spans/words/lines, scoped extraction/profiles, classifications, structured output/diagnostics, search; strictly offline OCR with verified local models or explicit unavailable status |
| Integration | Root owner | Shared supervisor/registry, CLI/API/GUI contracts, credential transport, capability reconciliation, preservation, whole-suite validation, source/output evidence and existing PR12 checkpoint |

Each module supplies a detailed capability JSON with observed outcomes, tested
paths, upstream no-ops and unsupported/gated APIs. The
[entry-by-entry coverage map](pdf-oxide-api-coverage.json) accounts for all 788
inventoried entries and links their family reports. Aliases, value protocols,
constructor helpers and blocked methods do not count as independent completed
features. This accounting does not certify correctness or parity.

The Rust public surface also includes parsing/lexer/object/xref/decoder internals,
layout/content/font/text/pipeline/structure/hybrid modules, color/crypto,
conditional OCR/rendering/signatures/parallel support, FFI, WASM, font/CMap
caches and image handles. They are cross-checked against Python bindings, not
advertised as app operations when absent from the pinned Python distribution.
Async aliases use the app's existing bounded queue/process supervisor rather
than adding a second unbounded batch service.

## Verified dependency and execution contract

`uv.lock` pins `pdf-oxide==0.3.78` and all platform artifact hashes. The inspected
Linux x86_64 wheel SHA256 is
`0b7e03008ca441e9183ef8f3c5d895424aca8119d5a860a205e1d16649f4f6a8`;
the source distribution SHA256 is
`18fb43c7b4e390400aab51f546a7b8c6ee471b5cbec88f4cf3dd74f6c43c49f2`.
The standard CPython 3.14.8 ABI3 wheel executed successfully. Free-threaded
Python, other platforms and a source-to-wheel reproducibility claim are not
qualified. The PyPI upload did not use Trusted Publishing. The dependency's
MIT/Apache-2.0 texts and NOTICE are shipped under `notices/pdf_oxide`; the pinned
Office dependency's notices are under `notices/office_oxide`. The app's own
license remains unchanged. The optional OCR runtime is `onnxruntime==1.31.0`;
its presence does not qualify inference or supply models.

All native processing requires nonroot Linux, an enforced address-space ceiling
at most 2 GiB, finite CPU/file/process/descriptor limits, seccomp network and
process-group restrictions, and the existing parent wall-time/cancellation/
cleanup supervisor. Required unavailable bounds fail closed. This is not a
filesystem sandbox. Parsing is confined to explicit regular-file snapshots;
native allocation can occur before result-level page/image/cell limits are
checked, so the kernel memory limit remains essential.

Default adapter limits are 50 MiB per input, 2,000 source pages, 100 selected
pages, 16 MiB cumulative serialized output, 500,000 extraction characters,
200 tables, 50,000 cells, 100 images, 25 million pixels per image, 50 million
total image pixels, 8 MiB per PNG and 16 MiB total image bytes. Whole-document
cleanup is capped at 100 pages. Other full-document operations declare tighter
family-specific limits. Limits cause explicit failure rather than published
truncation. The existing session accepts at most 200 files and 500 MiB; one
active job owns the parser queue and failures are recorded per file.

Passwords travel through private prompts/HTTP JSON to the worker's stdin,
never subprocess argv. Declarative Oxide request JSON is capped at 256 KiB;
legacy requests keep their 32 KiB limit. Browser secret controls clear before
submission; CLI secrets require prompts. No default model downloads, automatic network resources,
MCP installation/registration, persistent services, private keys, TSA requests,
embeddings or vector stores are enabled by this integration.

## Accuracy findings already observed

Direct native cells match the original A1/B1 three-row fixture and the missing
borderless-cell fixture. Both ruled and borderless wrapped descriptions are
split into an extra row. A ruled empty-row fixture loses its header and empty
row. These failures are preserved, not repaired by changing the oracle. Numeric
prose controls remain separate from table candidates.

Cleanup operates on a separate document instance after retaining original text
and cells. Repeated meaningful headings and citations can be removed from the
derived copy. Tagged pagination handling can return early and leave untagged
pages untreated. The threshold is not a semantic correctness score. Cleanup
defaults off; no standalone whitespace-normalization API is invented.

HTML uses explicit page calls and app-owned source-hash/page IDs. Raw native
HTML is saved as inert text/JSON; the visual wrapper allows only structural tags
and removes all native attributes, CSS, active content, links and images.
Native HTML tables are not substituted for `extract_tables()` rows/cells.
Images default off; inspection hashes native PNG bytes in memory. Only explicit
export writes PNGs. Empty image results do not prove an image-free document;
malformed or unsupported streams may be silently omitted upstream. Pixel,
mask, transparency and CMYK fidelity require comparison against the source.

The complete 72-fixture extraction capture has 72 process completions but only
38/50 scored exact ordered-text matches, 20/35 scored exact table-cell matches,
and 12/15 successful prose-without-table controls. Unscored cases are not passes.
All source/oracle hashes and failed outputs remain preserved.

Independent review found text replacement/removal painting black masks,
generated table text crossing grid borders, HTML/CSS table cells overlapping,
invalid native highlight appearance, nonpersistent annotation removal, and
native radio creation defects. These paths fail explicitly. Region-filtered text
can reinsert whole-page tables outside the region and is blocked; region spans,
words and direct table/image metadata remain separate bounded calls. Plain-text
format conversion ignores artifact exclusion on the frozen tagged fixture, so
that option combination is blocked; Markdown exclusion was exercised correctly.

Native AES256 encryption produces a dictionary rejected independently by qpdf;
authenticated editor rewriting also fails on encrypted inputs. These results
are discarded. Signature verification does not establish certificate trust or
revocation, and padded/unlinked signatures remain explicit gaps. Sanitization
can retain associated files or page actions and returns partial. PDF/A samples
substitute Nimbus fonts; native conformance checks are not certification.
XLSX/PPTX samples lose vector graphics/table borders; affected outputs are partial.
Raster flattening changes paper/margins and requires explicit acknowledgments.
Direct CMYK image embedding is blocked unless explicit RGB normalization is
chosen, with both input hashes and its color limitation reported.

The initial extraction stage passed 27 focused tests and a subsequent local
667-test suite including real Chromium tests. That run predates the expanded
modules and does not qualify them. Final full-scope results and source/output
artifacts are recorded separately. Parent Ada inspected the 20-page checkpoint
review and independently rendered three failed derivatives with Poppler, confirming
the table/HTML/text defects. Simple numeric/missing-cell samples looked correct;
image fidelity and form semantics were not accepted. See the
[hash-bound review record](PDF_OXIDE_REVIEW_CHECKPOINT_20261015.md).
General missing-text/number/table accuracy acceptance remains pending. PR12 remains on
merge hold; no family in the existing 198-feature reconciliation is silently
promoted.

## Source authorities

- [Pinned PyPI release](https://pypi.org/project/pdf-oxide/0.3.78/)
- [Pinned upstream source](https://github.com/yfedoseev/pdf_oxide/tree/v0.3.78)
- [Native parsing security limitations](https://raw.githubusercontent.com/yfedoseev/pdf_oxide/main/SECURITY.md)
- [Python API](https://pdf.oxide.fyi/rust/docs/reference/python-api)
- [HTML extraction](https://pdf.oxide.fyi/rust/docs/extraction/html)
- [Image extraction](https://pdf.oxide.fyi/rust/docs/extraction/images)
- [Cleanup API](https://pdf.oxide.fyi/rust/docs/reference/rust-api)
- [Office conversion](https://pdf.oxide.fyi/rust/docs/creation/office-conversion)
- [Page-first extraction and batch examples](https://pdf.oxide.fyi/rust/docs/guides/pdf-for-rag)

Documentation defaults can differ from pinned bindings. Installed signatures,
release source and actual bounded execution determine each exposed contract.
