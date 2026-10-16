# PDF Oxide correction review — merge hold

Parent Ada reviewed every page of the 27-page correction PDF and five-page image
comparison PDF for checkpoint `45dc3224054ad30f08b4dce2385a4d4eaba02dc0`.
She independently verified the correction archive SHA-256:
`680928ba58837c1177211d2d8c4cff041e553b346a2f45bb47840bff141eab3b`.
Earlier packages and their failures remain preserved.

Ada independently opened the old unsafe redaction derivative with PyMuPDF.
Ordinary extraction omitted the secret, but decoded xref 7 retained synthetic
`ACCOUNT0042TOTAL987.65` in `/ActualText`. The native redaction operation remains
blocked. The old derivative is failure evidence, not a usable redaction.

Ada visually confirmed the CMYK color error, RGBA opacity loss and malformed
image omission. The inline two-color example looked retained. Geometry-preserving
composition restored dimensions, rotation and numbers in the reviewed cases,
but changed fonts and spacing. Highlight rectangles became visible across
rotations; this does not establish semantic text alignment. Image fidelity,
creation/render parity and general appearance correctness remain unaccepted.

## Selected-page flattening failures

Ada identified two further source-derived risks in the pinned native editor:
annotation/form overlays were attached only when page `/Contents` existed, and
new local `/Resources` dictionaries could shadow inherited resources. The
adapter at `45dc322` checked selected-page geometry and annotation removal but
compared original content/resources only on unselected pages.

Both risks were subsequently reproduced through supervised Jobs on that exact
checkpoint, separately for `oxide_annotation_flatten` and `oxide_form_flatten`:

| Synthetic input | Faulty completed output | Independent evidence |
| --- | --- | --- |
| Valid appearance, no page `/Contents` | Selected page is entirely blank after annotation/widget removal | Both MuPDF and PDFium show 76,800 nonwhite source pixels becoming zero. The appearance stream survives in resources but no page content invokes it. |
| Font and image in inherited `/Resources` | Local XObject-only resources hide the original font and image | Both renderers lose the colored image; MuPDF changes the invoice font and reports missing `Swatch`. The invoice's extracted numeric text still survives. |

The source amounts are `000123.40` and `20.15`. The unselected control page and
its `001.20` balance remain unchanged in all four cases. Source/output PDFs,
both renderers' page images, extracted text, decoded objects, baseline source
snapshot and hashes are retained outside the repository in the separate
selected-page review package. These are synthetic tests, not representative
private-document qualification.

The [pinned native editor](https://github.com/yfedoseev/pdf_oxide/blob/v0.3.78/src/editor/document_editor.rs)
contains the annotation path at lines 2781–2843 and the corresponding form path
at lines 2936–2964. Reproductions, rather than these source pointers alone,
establish the failures described above.

The corrected adapter rejects both reproduced shapes before native flattening,
using raw page dictionaries with inherited-attribute materialization disabled.
Saved selected pages must retain the exact original content-stream prefix,
resource entries and page Group. Normal appearance content/resources, placement,
multiplicity and order must match their actual appended draw commands. An
appearance object present only in resources is insufficient.

Supported inputs are deliberately restricted. Original page commands must be
inside balanced graphics-state blocks with balanced text state. Nonidentity
appearance matrices, unsupported placement and unverified appended commands
reject publication. Missing-appearance links require explicit zero-width border
properties without a conflicting visible width. Painted annotations require
usable normal appearances. Bounded validation failures publish no PDF. These
restrictions can reject otherwise valid PDFs; no hidden fallback changes them.

Independent review of the in-progress verifier also reproduced a hidden
annotation (`/F=2`) becoming visible after flattening. An independent rerun of
the identical source through an isolated archive of exact `45dc322` subsequently
confirmed this fifth baseline failure in both MuPDF and PDFium. The source,
faulty output, archived implementation and review images are preserved separately
from the four missing-content/resource cases. Earlier working-tree probes
remain explicitly labeled as lacking a pre-invocation implementation hash.
Visibility-affecting flags and unsupported external opacity/optional-content
properties now fail before native flattening. The opacity probe did not
demonstrate a rendering defect; its refusal is an unsupported-shape restriction,
not an additional confirmed native failure.

A supported form sample preserves its original body, resource graphs, appearance
commands and numeric content but fails exact PDFium pixel comparison in the
field glyph region: 3,941 pixels differ, with maximum channel difference 84.
MuPDF's source/output pixels match in that sample. The failed strict pixel oracle
and both images are retained; this result is not counted as form appearance
parity. PDFium page-text extraction omits source widget text even when it is
visibly present, reinforcing the need for source/output visual review.

Successful parsing, extracted-text agreement and annotation removal are insufficient.
The correction does not repair the native engine, reconstruct lost content,
qualify all appearance semantics or promote any of the 198 feature families.
Ada subsequently viewed all 13 pages of the selected-page review for checkpoint
`31a2909316286c9ca9f9b2bf80f3f1c3624fd79e`. She visually confirmed blank
missing-Contents results, lost inherited images/font changes despite retained
numeric extraction, and the hidden annotation becoming visible. The supported
control looked preserved, but the 3,941-pixel PDFium difference remains
unaccepted and its oracle remains unchanged. Her review record is retained by
the parent. Merge and release holds remain in effect; no PR title, body or
comment update is authorized here.

## Flatten transform precision loss

Ada then identified a source-derived numerical risk: the pinned native
`generate_flatten_overlay` writes scale coefficients with six fractional digits,
while the adapter compared those dimensionless coefficients with an absolute
0.001 tolerance. The risk was subsequently reproduced against an isolated
archive of exact `31a2909316286c9ca9f9b2bf80f3f1c3624fd79e`, with the invoked
adapter hash recorded before execution. Both annotation and form routes
incorrectly completed these finite synthetic cases:

| Appearance BBox | Intended rectangle | Saved scale | Actual extent |
| --- | --- | --- | --- |
| 1,000,000,000 square | 100 points square | 0 instead of 0.0000001 | Zero; the amount table disappears |
| 70,000,000 square | 100 points square | 0.000001 instead of about 0.000001428571 | 70 points square; the amount table shrinks |

MuPDF and PDFium both confirm the visible defects. The yellow two-row table
contains `000123.40` and `20.15`; original body text, colored image and the
unselected control page remain unchanged. Original PDFs, wrongly completed
outputs/reports, decoded commands, text, both renderers' images and hashes are
preserved in the separate precision review package. These are now runtime
findings, not source-only inferences. The two 1,000-unit BBox controls retain
their 100-point extents; the form control still differs in PDFium glyph pixels
and receives no appearance-parity claim.

The saved-result gate now requires positive finite axis scales without shear or
reflection. Using exact rational arithmetic over parsed PDF numbers, it checks
both the source and saved appearance BBoxes: width, height and all four
transformed corners must match the source annotation rectangle within
`min(0.00001 * rectangle extent, 0.001 / UserUnit)` per axis. The physical
ceiling is 0.001 point; small rectangles receive a smaller relative tolerance.
Saved appearance matrices and any extra invocation matrix must be exactly
identity. These checks prevent zero-scale collapse, nonzero scale error,
misplaced offset BBoxes and a collapsed saved clipping box from passing a loose
coefficient comparison. They run before any PDF is published and do not change
the native output. All four preserved failing originals now reject with zero
results; exact nonzero small scales remain supported in regression tests.

The first large-offset arithmetic test accidentally lost its intended decimal
precision in pikepdf's Array constructor. Its failed run is preserved. The
test now parses the original decimal tokens directly; neither the expected
failure nor the existing pixel oracle was relaxed. New precision-package
review by Ada remains pending. No feature-family acceptance is promoted.
