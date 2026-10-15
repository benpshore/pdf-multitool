# Explicit reading order — merge hold remains

This records the earlier `9e8fa46` flat-order checkpoint. The subsequent
[structured-grid work](STRUCTURED_TABLE_QUALIFICATION_20261015.md) adds actual
cells and a bounded automatic path; it does not turn this earlier result into
table qualification.

The frozen borderless-table cases are untagged. The historical shared exporter
splits distant same-row cells into separate blocks and prioritizes vertical
gutters. Both tables become `A1 A2 A3 B1 B2 B3`, while the two-column prose controls
retain their intended column-first order. Token shape, length, numeric content
and fixture names are not a defensible semantic classifier.

This change adds an explicit `reading_order` contract to native HTML, Markdown
and structured text export. The CLI accepts `--reading-order rows|columns|auto`,
the HTTP API accepts the same enum, and the real GUI exposes the same choice.
The default is `auto` and retains the prior algorithm and its two known table
failures. **This is an author-selected remedy, not a repair of automatic table
inference.** No original failing output is relabelled as success.

Explicit modes preserve every existing parser line, span and repeated occurrence.
They partition line bounding boxes by horizontal-first (`rows`) or vertical-first
(`columns`) non-crossing whitespace cuts. Balanced cuts and an iterative traversal
avoid a row-count dependence on the legacy inference recursion cap. The code does
not inspect fixture paths or use text/numeric shapes to decide table semantics.
Groups without a legal geometric cut retain their parser-relative order.

Explicit modes reject rotated pages, non-horizontal writing, strong bidi content
and nonfinite/malformed bounding boxes. Failed supervised jobs publish no results
and preserve source bytes; a valid subsequent job must succeed. The selection
applies to every page. Mixed layouts, wrapped cells, missing-cell identity and
overlapping objects remain unqualified. Report metadata keeps
`semantics_verified=false`, and job/export warnings state these boundaries.

## Evidence and acceptance distinction

The unchanged eleven representation expectations and thirteen LTR boundary
contracts are preserved. Both original table cases now satisfy their original
full strings and all three row-pair adjacencies under explicit `rows` in TXT,
Markdown and HTML. Default-mode tests still require the exact historical table
failure to remain visible. Existing prose and repeated-Form controls are retained.

Fifteen new frozen PDFs cover numeric cells, long paired records, short column
prose, missing cells and identical-byte ambiguity, each in three emission orders.
The same ambiguous PDF is tested against both explicit user policies. It is not
counted as automatically classified. A separate forty-row numeric regression
checks tight row spacing and avoids truncation at the historical recursion depth.
See `experiments/row_order_contract_v1/README.md`.

`scripts/qualify_reading_order.py` runs 21 actual GUI jobs across all three export
formats, downloads and compares published bytes, retains the exact source PDFs,
and renders each output. Source references use independent PDFium at 72/144/288
DPI. It also checks reload without resubmission, a 390-pixel layout, source hashes
and browser errors. Automatic-mode table exports are captured as known semantic
failures even though their processes complete successfully.

Ada's own source/output review remains `not_reviewed`. Neither scripted visual
capture nor test success is visual acceptance. Source-built Poppler 26.10 and
representative non-synthetic accuracy remain outstanding. The previous inventory
continues to declare zero fully qualified feature families. No dependency,
permission, license, deployment or release setting is changed.

Validation counts and the exact checkpoint are recorded in the PR handoff and
the hash-bound visual evidence package; the original historical evidence is not
rewritten to contain new outcomes.
