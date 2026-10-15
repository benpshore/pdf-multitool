# Results and limits

Inputs/contracts were frozen at `175c086c7c3b673c5ac5f7a5aff2a9580ce68350`,
based on main `1bfbe98740e75b534ce6c9ef87a88aefbf39742d`. The 97 files under
observations were captured with that main implementation, before the guard
change. Repeating the complete capture after the guard produced identical
bytes in all 97 files. Input hashes remained unchanged.

## Real-PDF observations

All 13 actual PDFs met their declared **local** ordering contract. Two cases
were reordered: the ordinary positive pair and closely adjacent alphabetic
labels. Eleven cases retained raw local order. The latter is a policy outcome,
not a semantic claim that content order is inherently correct.

| Cases | Raw extraction and local result |
| --- | --- |
| Positive reversed objects | BETA, ALPHA becomes ALPHA, BETA under the chosen spatial contract. |
| Mixed Latin/Hebrew | MuPDF coalesces the two text objects into one mixed line; local repair does nothing. This does not isolate its bidi guard. |
| Mixed within one word | Two parser lines; the mixed-bidi fragment is excluded directly. |
| 90/180/270 text rotations | Page rotation is zero; line directions identify actual rotated text. All retain order. |
| Superscript-shaped text | Raised 7pt BETA versus baseline 10pt ALPHA; MuPDF flags are 8, without the superscript bit. Size/baseline/bounds gates still exclude it. |
| 1pt baseline offset | Both 10pt; unequal origins and bounds exclude it. |
| Overlap | BETA starts 6pt before ALPHA's right bound; excluded. |
| A1/B1 paired labels | Alphanumeric text excluded despite a word-sized gap. |
| NAME/VALUE, 6pt gap | Accepted by the spatial contract; still semantically ambiguous labels, not proven prose. |
| NAME/VALUE, 8pt gap | Excluded by the local maximum-gap gate. |
| NAME/VALUE, 60pt gap | Local order retained; existing global gutter logic separates/orders the blocks. This is not a new local-repair effect. |

Model visual inspection covered the complete contact sheet at 72 DPI and all
four 216-DPI contact sheets. The renders show visible Hebrew glyphs, the three
rotation orientations, smaller raised BETA, a subtler 1pt baseline offset,
visible glyph overlap, and near/wider/column label spacing. No clipping or
missing-glyph boxes were observed. These are geometry/appearance observations,
not independent human semantic adjudication. Mixed-direction fixtures are
unshaped glyph sequences; correct linguistic bidi reading is not asserted.

## Safe malformed-coordinate probes and narrow repair

For an otherwise qualifying block extracted from positive_reverse.pdf, unit
inputs inject NaN, +infinity and -infinity into each block bbox coordinate,
each line bbox coordinate, and each span bbox/origin coordinate. Malformed
numbers are never sent to the native PDF engine.

Before the repair: 12 failures, 73 passes across 85 new cases. All failures
were supplied **block** bbox coordinates: the old helper reordered lines even
though their enclosing bounds were nonfinite. The 60 line/span-coordinate
probes already retained the original block; all 13 real-PDF contracts passed.

The four-line production change rejects a supplied block bbox unless it has
four finite coordinates. It adds no sorting, geometry inference, dependencies,
or changes to column thresholds. Missing block bboxes keep prior helper
behavior; real text blocks contain bboxes. This is defensive unit-level
hardening, not evidence of a parser-emitted nonfinite bbox or an exploit.

After the repair: all 85 new cases and all 43 PR10 focused cases pass. The
complete real-PDF capture is byte-identical before/after. Original input PDFs
are read-only and checked by SHA-256. PR8 and PR10 frozen artifacts are intact.

## Reproduction

Use the repository's existing locked development environment:

```sh
uv run --locked --extra html --extra ocr pytest tests/test_ltr_pdf_boundaries.py tests/test_baseline_ltr.py
uv run --locked --extra html --extra ocr python experiments/ltr_boundaries_v1/measure.py --output /tmp/new-boundary-capture
```

The generator requires the recorded DejaVu Sans font (provided by the existing
CI system package); it refuses to replace differing frozen bytes. Tests use
committed PDF bytes, not regenerated fonts. Engine: PyMuPDF 1.26.7 / MuPDF
1.26.12. No cross-engine or universal semantic accuracy is claimed.
