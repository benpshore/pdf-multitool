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
Ada's review of the new correction package remains pending. Merge and release
holds remain in effect; no PR title, body or comment update is authorized here.
