# Frozen PDF representation experiment, v1

This is an experiment checkpoint, **not an algorithm fix or an accuracy benchmark**.
Production code, dependencies and workflows remain exactly as at
`4ed4f914fe11024cb8771734afbda6747f6521e3`. No merge is requested by this work.
All eleven PDFs are synthetic, generated directly from explicit PDF operators.
No private PDFs, model inference or network artifact downloads are involved.

## Protocol and provenance

The generator, eleven PDF byte strings and author-defined expectations were frozen
and published in commit `ac6a1d1fedbd60f5e34d6fe9f7a8ca01af46d211` **before any
measurement of these fixtures with PyMuPDF or analysis.py**. The generator uses
only Python's standard library. Courier at 10 points gives exact six-point glyph
advances, so a `TJ` adjustment of -600 replaces an explicit space geometrically.

- [expectations.json](expectations.json): predeclared text order and rendering groups.
- [FROZEN.json](FROZEN.json): generator/expectation/PDF SHA-256 hashes.
- [observations/summary.json](observations/summary.json): complete pixel hashes,
  intermediate counts/orders, expected-vs-observed results and runtime versions.
- Every case directory contains RGB renders at 72/144 DPI, `pymupdf.json`
  (dict/rawdict, words, texttrace, bboxes, XObjects and fonts), `application.json`
  (analysis and split/ordered blocks), and the full text/Markdown/HTML exports.

Runtime: CPython 3.14.7, PyMuPDF 1.26.7, Linux. Exact runtime/build strings and
analysis.py/script hashes are in summary.json. Rendering used RGB, no alpha and
no annotations. Hash equality refers to complete pixel samples, not PDF bytes.
It establishes equivalence at the two measured raster resolutions in this
renderer; it is not a proof about all viewers, resolutions or accessibility trees.

The first measurement attempt stopped because the harness expected `[Page 1]`
instead of the exporter's actual `[Source page 1]` preamble. Only that instrumentation
literal was corrected. No fixture, frozen expectation, production code, word
normalization rule or semantic oracle was changed. The interrupted output was
kept locally at `/tmp/representation-first-attempt-preamble`; the published capture
is the completed run. A fresh second run reproduced **every published observation
file byte-for-byte**, including renders, JSON and all three export formats.

## Results

All four groups have equal pixel hashes at **both** 72 and 144 DPI.
Eight fixtures match the predeclared ordered-text expectation; three do not.
That count describes this small designed set and must not be reported as an
accuracy percentage or representative-corpus estimate.

| Fixture or comparison | PyMuPDF grouping / provenance | Application result |
| --- | --- | --- |
| Explicit space vs `TJ` gap | Both expose one block/line/span and `ALPHA BETA` in dictionary text. The rawdict space is `synthetic: false` for the explicit space and `true` for `TJ`; texttrace has no space character for `TJ`. | Both match `ALPHA BETA`. Text equality hides different provenance. |
| Split `Tj` chunks in one text object | Coalesced into one block/line/span, like the baseline. | Matches; no sensitivity demonstrated for this fragmentation. |
| Two forward text objects | One block/line/span; inferred synthetic space, two texttrace spans. | Matches `ALPHA BETA`. |
| Same objects emitted in reverse order | Same pixels; one block containing two same-baseline lines/spans, first BETA then ALPHA. | **Mismatch: `BETA ALPHA`.** Both block ordering and structured text preserve the wrong order. |
| One Form used twice vs two expanded placements | Reused fixture has one XObject resource but two texttrace occurrences and two dictionary blocks. | Both preserve `STAMP STAMP`; no occurrence was collapsed. |
| Two-column prose, row- vs column-emission order | Row emission gives three row blocks/six lines; column emission gives six blocks. | Both become left-column-first and match the authored prose order. |
| Borderless table, row- vs column-emission order | Row emission initially exposes the intended paired order in three blocks; column emission exposes six column-ordered blocks. | **Both mismatch:** `A1 A2 A3 B1 B2 B3`, rather than `A1 B1 A2 B2 A3 B3`. |

See [reverse-object evidence](observations/objects_reverse/application.json),
[spacing provenance](observations/space_tj/pymupdf.json),
[prose control](observations/prose_row_operators/application.json) and
[table control](observations/table_row_operators/application.json).

## What these results isolate

1. **Representation sensitivity and semantic failure are different questions.**
   The reversed-object pair is a representation-sensitivity counterexample:
   identical measured pixels produce different application ordering. The table
   pair is representation-invariant yet fails the author-defined semantic oracle.
   Passing invariance alone cannot establish correct reading order.
2. **Intermediate grouping is an input to the algorithm, not ground truth.**
   Reversing word operators changes PyMuPDF's one-line grouping into two lines
   within one block. Their six-point horizontal gap is below the application's
   `max(18, width * .035)` split threshold (21 points on these pages). No block
   split occurs; ordering a single block leaves its parser line order intact.
   This traces the observed failure through both parser and app stages. It does
   not establish that all reversed operator streams fail.
3. **The same geometric rule helps prose and hurts paired records.**
   `_split_column_blocks` turns both row-emitted controls into six blocks.
   `_reading_order` then prioritizes a vertical gutter, producing column-first
   output. That repairs the prose control but destroys the table's intended row
   pairing. The table's oracle comes from its author, not PDF tags: these fixtures
   do not establish that author intent is uniquely recoverable from geometry.
4. **Character observations are not authored-character completeness.**
   The explicit and `TJ` inputs both yield ten reported text-layer characters,
   although texttrace has ten vs nine characters and rawdict marks the inferred
   space. The reversed object representation reports nine characters. The existing
   app caveat that counts describe parser observations is therefore material.
5. **Resource identity is not occurrence identity.**
   One reused Form is rendered and extracted twice here. Any future deduplication
   based solely on XObject identity would lose visible content. This positive
   control does not cover nested Forms, clipping, rotations or overlapping copies.

These observations do not justify simply lowering gutter thresholds, globally
sorting every line by x/y, or deduplicating equal strings. Such changes could
repair one designed case while damaging prose, bidirectional text, mathematics
or repeated placements. The next experiment should freeze held-out controls for
those competing interpretations before proposing an algorithm change. Preserving
provenance and exposing uncertain ordering are candidates to evaluate, not changes
made in this checkpoint.

## Reproduce without changing the oracle

From the repository root:

```sh
uv sync --locked --extra html
uv run --locked python experiments/representation_v1/build_fixtures.py --check
uv run --locked python experiments/representation_v1/measure.py --output /tmp/representation-fresh
diff -qr experiments/representation_v1/observations /tmp/representation-fresh
```

Use a nonexistent output directory; the harness refuses overwrites. An identical
environment should reproduce files exactly. Different Python/platform metadata
will change summary.json; renderer/build changes can also change pixel hashes.
Inspect those differences rather than updating the frozen oracle to obtain a pass.
The measurement command returns success when capture completes even when semantic
expectations fail; failures remain explicit in the summary and this report.

The original product suite passed locally: 264 tests, one optional browser skip,
371 subtests. Ruff, TypeScript, Spectral and ShellCheck passed. Local OSV remained
tunnel-blocked; hosted required CI is reported in the PR/handoff. Product behavior
was not modified, and the parent's PR3–7 merge stack was not touched.
