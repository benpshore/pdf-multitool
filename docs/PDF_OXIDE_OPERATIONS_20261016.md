# PDF Oxide operation status and adapter repair scope

Baseline: [`f752ed807f1626791b4d87d7caab6620b77953fe`](https://github.com/benpshore/pdf-multitool/commit/f752ed807f1626791b4d87d7caab6620b77953fe), PDF Oxide 0.3.78.
This is the exact 37-operation catalog, with dispositions updated for the
2026-10-16 correction pass. The baseline's failures remain preserved; any
additional processing is identified separately.

**Working, bounded** means an implemented route has exercised supported cases;
it is not general accuracy, visual or standards qualification. **Partial** means
the route has known content/appearance limitations or blocked submodes.
**Blocked** means the requested path fails explicitly. **Unimplemented** means
there is no app workflow for that capability, even if a helper type is present.
No status here promotes a family in the 198-family acceptance ledger.

| # | Exact operation | Correction disposition | Important boundary |
| --- | --- | --- | --- |
| 1 | `oxide` | Partial | Native text/table candidates retain known ordering, wrapped-row, empty-row and false-table failures. Cleanup can remove meaningful content. Explicit export previews show the exact PNGs; CMYK color and soft-mask loss remain native failures. Actual page-count selection and diagnostic partial flags corrected. |
| 2 | `oxide_format` | Partial | Bounded text/Markdown/HTML with original baseline. Native truncation/omission/OCR diagnostics now mark affected pages and job partial. Plain-text artifact exclusion is blocked because native ignores it; HTML has no such native control. |
| 3 | `oxide_create_text` | Working, bounded; submodes blocked | Text, Markdown and restricted HTML/CSS. Independent content/structure checks reject detected clipping, Unicode/numeric loss; native HTML tables blocked. Parse/content checks do not qualify visual fidelity. |
| 4 | `oxide_create_document` | Working, bounded; submodes blocked | Explicit text, shapes, fonts/images and supported fields with independent integrity/content checks. Native tables, streaming tables, radio groups, broken markup/stamps, named links and lost keywords are blocked. |
| 5 | `oxide_create_barcode` | Working, bounded | Inert native barcode/QR SVG; decoded payload still requires verification for the intended reader/use. |
| 6 | `oxide_create_pattern` | Working, bounded fragment | Pattern cell content exported as text; not a complete PDF or a rendered pattern consumer. |
| 7 | `oxide_edit_inspect` | Working, bounded | Structure, geometry, text/image elements, outlines/layers/labels/XMP inspection. Native `get_element` stub is not promoted to usable editing. |
| 8 | `oxide_edit_pages` | Working, bounded | Select, extract, delete and move pages; encrypted rewriting blocked; cross-page semantics need review. |
| 9 | `oxide_edit_ranges` | Working, bounded | Inclusive nonoverlapping ranges into separate derivatives. |
| 10 | `oxide_edit_geometry` | Working, bounded | Explicit page boxes/rotation; cropping hides content, without erasing it. |
| 11 | `oxide_edit_metadata` | Working, bounded | Info fields; existing XMP may disagree. No independent XMP mutation API. |
| 12 | `oxide_edit_text` | Partial | Add text with independent glyph checks. Replacement/removal blocked because native paints black masks. |
| 13 | `oxide_edit_images` | Working, bounded | Change named image placement; no claim to replace/re-encode original pixels. |
| 14 | `oxide_edit_embed` | Working, bounded | Explicit attachment bytes; ten files/64 KiB total. No execution. |
| 15 | `oxide_edit_bookmarks` | Working, bounded | Plan or export bookmark segments; missing outlines fail. |
| 16 | `oxide_edit_merge` | Working, bounded | Up to 20 PDFs in requested order; cross-document form/outline/destination semantics unqualified. |
| 17 | `oxide_form_inspect` | Working, bounded | AcroForm data and widget pages; XFA presence only. |
| 18 | `oxide_form_fill` | Working, bounded | Supported existing AcroForm values; independent saved-value checks. XFA/signatures and unsupported fields rejected. Appearance is separate acceptance. |
| 19 | `oxide_form_export` | Partial | Exact JSON plus bounded FDF/XFDF; Unicode FDF and lossy XFDF array/empty-string cases are blocked. |
| 20 | `oxide_form_flatten` | Partial; known-loss cases blocked | Missing content streams/inherited page resources fail before native flattening. Saved widget removal, selected original content/resources, invoked appearances and nonselected-page preservation are checked independently. Native loss warnings and unsupported placement reject publication; no general appearance qualification. |
| 21 | `oxide_annotation_inspect` | Working, bounded | Native metadata/rectangles; links remain inert strings. |
| 22 | `oxide_annotation_edit` | Partial; explicit correction available | Notes/links work in tested cases. Native highlight stays blocked by default; explicit pikepdf rectangle/appearance correction is separately identified with original native output retained. Nonpersistent removal rejected. |
| 23 | `oxide_annotation_flatten` | Partial; known-loss cases blocked | Missing content streams/inherited page resources fail before native flattening. Independent removal, selected original content/resources and appearance draw commands, plus nonselected-page preservation, are required. Unsupported appearances and persistent annotations reject publication. Widget pages require form flattening first. |
| 24 | `oxide_flatten` | Partial; geometry correction available | Native default still relayouts. Explicit `preserve_visible_page` composes native rendered PNGs at checked visible physical dimensions, normalizing crop/rotation metadata. Native rendering fidelity unqualified; no redaction claim. |
| 25 | `oxide_render` | Working, bounded | PNG/JPEG/premultiplied pixels/ink plates; physical geometry/color/layers require source comparison. Native fit sizing has crop/media-box limitations. |
| 26 | `oxide_office_export` | Partial | DOCX/XLSX/PPTX; sampled XLSX/PPTX drop vectors/table borders. Automatic positioned/flow thresholds recorded. |
| 27 | `oxide_office_import` | Partial | Bounded DOCX/XLSX/PPTX; macros/OLE/external relationships/encryption rejected. Native diagnostics unavailable; no independent Office renderer qualification. |
| 28 | `oxide_image_pdf` | Working, bounded; color gate | Image on native default paper/margins. Direct CMYK rejected unless explicit RGB normalization is selected and reported. |
| 29 | `oxide_images_pdf` | Working, bounded; color gate | Ordered bounded image pages on native default paper/margins; same color limitation. |
| 30 | `oxide_security` | Partial; rewrite modes blocked | Advisory permissions/signature/certificate/timestamp inspection works in scoped tests. Signature object identities reconciled; duplicate/unmatched enumeration stays partial. Encryption/decryption blocked; trust/revocation and real signing not established. |
| 31 | `oxide_redact` | Blocked entirely | Reproduced recoverable `/ActualText` and six q/Q text-state leaks despite overlays. No native redacted copy is published; catalog/UI unavailable. |
| 32 | `oxide_sanitize` | Partial | Selected catalog cleanup; associated-file and page/field-action carriers can remain. Not anonymization or a safe-document guarantee. |
| 33 | `oxide_compliance` | Partial | Native PDF/A/UA/X findings and PDF/A conversion; font substitution changes pixels. No standards certification. |
| 34 | `oxide_extract_mask` | Working, bounded derived copy | Explicit extraction masks, separate baseline; not PDF redaction. Header/footer masking can remove meaningful content. |
| 35 | `oxide_analyze` | Partial | Bounded geometry, vectors, layout and classifications. Filtered text is blocked because native can leak out-of-region table content; scoped spans/words/direct cells remain separate. |
| 36 | `oxide_search` | Working, bounded | Native text search with page/geometry identity and result caps; no OCR. |
| 37 | `oxide_ocr` | Blocked by resource/qualification gate | Requires verified compatible local models. Loader-negative tests pass; actual model inference and OCR accuracy remain unqualified. |

## Full API accounting

The [current full disposition file](pdf-oxide-api-coverage.json) accounts for
788 entries, 76 exports and these exact 37 operation names. All seven family
report hashes and implementation snapshots are checked; no classification is
unresolved. Current counts are 295 implemented-with-constraints entries,
28 upstream defects/no-ops and two helpers belonging to a blocked workflow;
the other baseline categories below are unchanged. Complete accounting is not
complete implementation or accuracy acceptance.

The authoritative entry-by-entry files at the baseline commit are:

- [`docs/pdf-oxide-api-coverage.json`](https://github.com/benpshore/pdf-multitool/blob/f752ed807f1626791b4d87d7caab6620b77953fe/docs/pdf-oxide-api-coverage.json): 788 entries, 76 exports, exact report references, aliases and explicit dispositions.
- [`docs/pdf-oxide-public-api-0.3.78.json`](https://github.com/benpshore/pdf-multitool/blob/f752ed807f1626791b4d87d7caab6620b77953fe/docs/pdf-oxide-public-api-0.3.78.json): installed signatures/docs; inventory alone is not runtime qualification.
- Family reports: `docs/oxide-capabilities-analysis.json`, `docs/oxide-capabilities-conversion.json`, `docs/oxide-capabilities-creation.json`, `docs/oxide-capabilities-editing.json`, `docs/oxide-capabilities-format.json`, `docs/oxide-capabilities-forms.json`, `docs/oxide-capabilities-security.json`.

Baseline disposition counts are 299 implemented-with-constraints entries,
149 async workflow equivalents, 43 workflow equivalents, 19 value/constructor
equivalents, 102 internal/value protocols, 67 value-object members, 55 entries
with no bound workflow consumer, 26 upstream defects/no-ops, 20 resource or
active-content gates, five unqualified resource gates, two methods not exposed
as secure redaction, and one not exposed as trust validation. These sum to 788;
none is a feature-parity or accuracy score.

Unimplemented/binding gaps include usable rendering consumers for several
gradient/graphics-state helpers, XFA editing/rendering/export, FDF/XFDF import,
selectable Rust-only Office flow/layout variants, legacy Office import, general
image/vector/form redaction, and independent standards/trust validation.
Private-key signing, TSA access and OCR model provisioning are separate gates,
not implied by the presence of their SDK types. Known broken tables/highlights,
text replacement and encryption are upstream defects, not completed features.

## Adapter corrections and retained failures

1. **Image review panels:** explicit export now embeds the exact bounded PNG
   bytes, with a matching hash and no external requests. Inspect-only bytes
   remain ephemeral. The native CMYK color and soft-mask failures are visible
   and preserved; no pixel reconstruction or fidelity acceptance is claimed.
2. **Raster flatten page relayout:** explicit `preserve_visible_page` places
   native rendered pixels on their independently checked visible page extent.
   Reports record crop/rotation normalization and native methods. The original
   native relayout path and failure evidence remain; unsupported physical units
   fail, and renderer font/color differences remain unqualified.
3. **Highlight appearance:** explicit `allow_highlight_repair` uses pinned
   pikepdf for the measured rectangular QuadPoints/no-appearance defect. It
   preserves the source and unverified native output, identifies both engines,
   and checks existing page/content/resource/annotation preservation. Both
   independent renderers exercise the rotated/cropped synthetic cases. The
   defective native path stays blocked by default.
4. **Incorrect completion status:** actual native operator-cap omissions now
   produce page/file truncation and partial status for extraction, formatting
   and conversion. Known missing content is retained as evidence, not invented.
5. **Flatten postconditions:** missing page content and inherited resource
   shadowing caused four reproduced losses across annotation/form routes.
   Source preflight rejects those shapes. Saved original content/resource and
   appearance-reference/draw-command checks cover selected pages as well as
   nonselected-page preservation. Persistent annotations, unsupported placement
   and native blank-field loss warnings prevent publication. Formerly completed
   failing outputs remain in the separate inspection package; no native repair
   or general visual parity is claimed.
6. **Redaction and signature accounting:** the entire native redaction route is
   blocked after seven executed recoverability regressions. Signature coverage
   reconciles object identities; duplicates cannot substitute for unlinked
   objects, and ambiguous enumeration remains partial.
7. **Page selection and generated output checks:** `all` uses the actual bounded
   worker page count. Creation checks independent structure and requested
   content before publishing; converted PDFs receive an independent structural
   reopen. These checks do not establish order, cell association or appearance.

No safe semantic repair is asserted for inferred table row boundaries, missing
logical rows, prose/table classification, meaningful repeated headers/citations,
unimplemented Office vectors, encryption dictionaries, or black replacement
masks. Reassembling cells, guessing author intent, painting over content or
patching cryptographic parameters would conceal limitations. Those paths retain
their failures/gates until a separately verified implementation is available.

No PR metadata changes or additional public communications are authorized by
this technical repair scope. Existing source checkpoints, original evidence,
frozen oracles and merge/release holds remain in place.
