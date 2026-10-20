# Structured grid projection — merge hold remains

The earlier explicit-order checkpoint only changed a flat sequence. Parent Ada
reviewed its first three comparison pages and correctly found no reconstructed
table cells. That result is not table qualification.

## Automatic path and abstention

Native HTML, Markdown and structured TXT exports now call the pinned PyMuPDF
table finder. Drawn-rule grids are automatically projected only when the native
cell boundaries form a complete rectangular grid and each contributing source
line occurrence fits exactly one cell. Text is taken from the original parser
lines, not substituted from the detector's extracted strings. Numeric values
remain strings; leading zeros, signs, punctuation and repeated occurrences are
not discarded. No header role is inferred.

The resulting HTML contains real `table`, `tr` and `td` elements. Markdown uses
raw HTML tables with literal cell content, requiring a renderer that supports
raw HTML. TXT uses tab-separated records with quoted embedded line breaks;
decoding TSV preserves empty columns and wrapped cells. Fully empty ruled rows
are retained. A literal TXT viewer does not visually align multiline cells;
decoded record accuracy is not visual table-layout fidelity. A JSON companion
records cell rectangles, exact strings, source
line occurrence IDs, disposition and `semantics_verified=false`.

This validates a bounded geometric representation, not the document author's
semantic intent. Even a ruled layout can contain prose or non-tabular content.
No feature family is newly declared fully qualified.

Text-alignment detection is also run to expose borderless candidates. Both
PyMuPDF and pdfplumber text strategies misclassify the existing prose controls
as tables, create blank spacer bands and miss columns under their default
minimum-word setting. Their agreement is not independent semantic evidence.
The candidate path uses two supporting words per vertical alignment and merges
edges that cut complete source lines. It does not inspect fixture names,
numeric shapes, token lengths or particular words to infer table semantics.

**Auto does not promote borderless candidates.** It retains the original content
path and publishes `ambiguous_borderless` with candidate cells and a warning.
The two original borderless-table auto exports therefore still fail their
unchanged row-order oracle. A correct candidate matrix is not counted as a
correct primary export.

With explicit `rows`, separated borderless text bands can be rendered as a grid,
including empty column positions. These are labelled `user_selected_spatial_bands`.
Text-finder spacer bands are reported as omitted; missing logical rows cannot be
inferred from that spacing. Closely spaced bands (less than 1.5 times the largest
source line height) admit wrapped-cell interpretations and are not promoted even
with explicit rows. This conservative guard is not a semantic confidence score.
Wrapped borderless cells and entirely empty borderless rows remain failed cell
oracles in the evidence.

Explicit `columns` preserves its column-order path. Repeated-margin suppression
also retains the original text path so a table projection cannot silently bypass
the requested filter. Candidate decisions remain available in the JSON report.

## Preservation and boundaries

Rotated pages, merged/spanning native cells, clipped or multiply assigned source
lines, non-horizontal/bidirectional cell content and image-overlapping candidates
are not projected. A detector failure discards partial projections on that page
and leaves source content on the original path. Limits are 5,000 source lines and
10,000 drawing paths per page, and 500,000 inspected characters, 200 candidates
and 50,000 cells per document. Existing worker timeouts, cancellation, native
process/network guards and output quotas remain in force. No dependency,
permission or license setting changes.

JSON companions use exclusive creation and distinct names if a prior report
exists. Original input bytes and prior reports are preserved. The HTML remains
self-contained for viewing; the companion is evidence, not a rendering dependency.

The new reversed wrapped-prose counterexample exposed a separate flat-order
defect. After separating columns, a block with one common horizontal range,
matching typography, horizontal LTR writing and non-overlapping vertical lines
is now ordered top-to-bottom. Overlapping, mixed-style, superscript, bidi and
rotated cases retain the previous guarded behavior. This does not choose between
table and prose semantics.

## Evidence and outstanding acceptance

`experiments/structured_cells_v1` freezes 27 authored synthetic PDFs before
measurement: numeric values, missing cells, wrapped cells, fully empty rows and
numeric wrapped prose, each with three operator-emission orders. Both ruled and
borderless variants are retained. All earlier frozen experiments remain intact.

Cell tests decode actual HTML/Markdown grids and TSV records against complete
authored matrices, including literal newlines and empty cells. They also check
surrounding text, unique source occurrence assignment, literal markup, repeated
text, merged-cell rejection, detector/limit fallback and original-file hashes.
The frozen historical scorer is not changed; new TSV cases decode quoting as
syntax rather than deleting quote characters to manufacture a text match.

`scripts/qualify_table_structure.py` performs 36 actual GUI submissions and
downloads: 21 decoded primary cell-matrix matches, 12 unresolved primary table outputs,
and three prose-column controls. Those counts describe this synthetic capture,
not representative product accuracy. The capture uses independent PDFium source
renders at 72/144/288 DPI, native HTML, Pandoc-rendered actual Markdown and literal
TXT views. Every Ada acceptance field remains `not_reviewed`.

Automatic borderless semantic selection, wrapped/empty-row inference without
rules, merged-cell support, representative user-document accuracy, Ada's own
hash-bound visual review and source-built Poppler 26.10 remain outstanding.
No merge, release, deployment or broader toolkit-parity claim is implied.
