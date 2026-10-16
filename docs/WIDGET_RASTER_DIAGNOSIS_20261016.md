# Bounded diagnosis of two PDFium widget raster differences

At source checkpoint `5644fcaf9014928a817dfc0cd470991329e141ec`, neither retained
form control demonstrates native content or geometry loss. The measured
difference is explained by PDFium applying different raster options to interactive
widgets and ordinary page content. Ada accepted this diagnosis for these two
synthetic fixtures after independent checks and viewing all seven review pages.
No form qualification follows from this finding.

| Fixture | Default PDFium at 144 DPI | PDFium LCD at 72/144/288 DPI | MuPDF at 72/144/288 DPI |
| --- | --- | --- | --- |
| `retained-3941` | 3,941 changed pixels; maximum channel delta 84 | Exact source/native match | Exact source/native match |
| `precision-control-1238` | 1,238 changed pixels; maximum channel delta 83 | Exact source/native match | Exact source/native match |

The saved appearance bytes, complete resource/font graphs, BBox, Matrix, page
geometry and original page-content prefixes are preserved. An independently
composed page-content derivative and a diagnostic FreeText derivative both match
native output in all 15 PDFium mode/resolution combinations per fixture. Saving
the source after PDFium form initialization and rendering preserves its appearance.
Interactive appearance-region pixels ignore the caller's LCD, print and
no-smooth-text switches, while ordinary page text responds. Disabling form drawing
removes the entire appearance and is only a negative control.

Native output explicitly writes `/FormType 1`, the optional appearance-form
default omitted by the source. The portable graph verifier records raw
dictionaries and normalizes only that default for appearance comparison;
decoded appearance bytes and complete resource/font graphs match without it.

The independent reviewer verified all 90 packaged PDFium comparisons, 12 packaged
MuPDF comparisons, appearance bytes, complete resource/font graphs, geometry and
content prefixes. They also freshly rendered the 12 MuPDF comparisons with
MuPDF 1.26.6; every pair matched. The original capture used MuPDF 1.26.7.
Ada viewed all seven pages before accepting this narrow diagnosis. The review
statement and hashes are retained in the [experiment](../experiments/widget_raster_policy_v1/README.md).

## Mechanism and source limits

The pinned [PDF Oxide appearance extraction code](https://github.com/yfedoseev/pdf_oxide/blob/v0.3.78/src/editor/document_editor.rs#L6091)
regenerates an existing widget appearance when its field value has been modified.
These fresh-document flatten jobs do not fill values, and their decoded saved
appearance is unchanged. Native regeneration is therefore not demonstrated here.

The pypdfium2 render helper passes the same flags to page rendering and form
drawing. Current official PDFium source corroborates the observed discontinuity:
[PageView_OnDraw](https://pdfium.googlesource.com/pdfium/+/refs/heads/main/fpdfsdk/cpdfsdk_pageview.cpp#77)
passes the draw-annotations boolean to individual annotations,
[DrawAppearance](https://pdfium.googlesource.com/pdfium/+/refs/heads/main/core/fpdfdoc/cpdf_annot.cpp#469)
renders with null options, and
[RenderContext](https://pdfium.googlesource.com/pdfium/+/refs/heads/main/core/fpdfapi/render/cpdf_rendercontext.cpp#69)
retains defaults when none are supplied. The
[default options](https://pdfium.googlesource.com/pdfium/+/refs/heads/main/core/fpdfapi/render/cpdf_renderoptions.cpp#27)
enable ClearType. This is consistent with the measured LCD agreement.

The installed PDFium binary is 156.0.8076.0 through pypdfium2 5.14.0. Its metadata
does not identify an exact source commit, and the exact-version source fetch did
not succeed. The current official source is corroborating evidence, not a claim
to the binary's exact source revision. Historical GitHub source snapshots were
from an archived 2023 mirror, not that binary. Public identifiers and the binary
hash are retained in the experiment without raw environment paths.

## Unchanged acceptance boundary

The strict default PDFium oracle still fails and its original assertion and
evidence are retained byte-for-byte. LCD mode is a separate diagnostic; it does
not replace default rendering or weaken the oracle. No production code, tests,
renderer defaults or capability classification change in this evidence checkpoint.

The accepted statement is limited to these two fixtures. General form fidelity,
representative non-synthetic accuracy, and the broader merge, release, redaction
and appearance-fidelity holds remain unresolved. This diagnostic subtask ends
with the evidence checkpoint; it authorizes no further capability work.
