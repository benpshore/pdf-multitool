# Recoloring follow-up — 15 October 2026

This follows the SVG checkpoint `e180142ccdbd5cc7c2a66c400bd36550c36986b7` and
implements a bounded PY-036 route from the received 198-family inventory. The
work remains in the saved Linux cloud workspace and the existing PR 12 integration
carrier retains its merge hold. Parent visual acceptance is not claimed.

## Product behavior

`pdf-multitool recolor source.pdf --colorspace gray --allow-color-change` produces
a new PDF and JSON manifest. `rgb` and `cmyk` are also supported. The actual GUI
and loopback API use the same strict catalog options. Conversion affects every
page; a `pages` option is rejected, so the product does not promise isolation of
changes to shared resources across selected/unselected pages. Original files are
retained. The acknowledgment is required because conversion can permanently lose
color information and rewriting invalidates cryptographic signatures.

The pinned PyMuPDF 1.26.7 / MuPDF 1.26.12 implements
[Document.recolor](https://pymupdf.readthedocs.io/en/latest/document.html#Document.recolor)
by applying [Page.recolor](https://pymupdf.readthedocs.io/en/latest/page.html#Page.recolor)
to every page. The product maps gray/RGB/CMYK to 1/3/4 color components. Image
color spaces may be ICC-based with the requested component count; they are not
misreported as necessarily using DeviceGray/DeviceRGB/DeviceCMYK names.
No engine pin or dependency changed.

The worker limits conversion to 100 pages, 100,000 PDF objects and 500,000
characters in the preservation check. Resource checks bound 256 distinct image
resources and 1,000 visible image instances, including inline images. Image
dimensions are limited to a 16,000-pixel side and 25 million pixels per image;
resource pixels and visible-instance pixels are each limited to 200 million per
document. Native inspection and conversion also remain under the existing
process memory/CPU/time, network, cancellation and output limits.

After saving, the worker reopens the output and requires the same page count,
MediaBox, CropBox, rotation and extracted text hashes. All outputs for an input
are published atomically after these checks. The manifest records the output
hash, requested component count, page evidence, warnings and `not_reviewed`
visual status. This is not proof of semantic or visual identity for arbitrary PDFs.

## Synthetic verification

The generated three-page fixture has red/blue vectors, green raster content,
an additional transparent red image, Unicode text, a cropped/rotated page and a
declared blank page. A separate fixture contains an inline RGB image. The tests
check actual supervised conversion, preserved source bytes/text/geometry,
independent pikepdf content operators and image component counts, retained
one-component transparency masks, acknowledgment/option failures, bounded image
headers, page limits, password handling, output quotas and recovery. A grayscale
then RGB round trip explicitly demonstrates that lost colors are not recovered.

The first engine/HTTP run passed 24 tests. Its pikepdf deprecation warnings were
resolved by using the pinned `Page.get_images()` API. The combined targeted run
passed 25 tests in 29.76 seconds. The full suite passed **495 tests and 374
subtests in 212.16 seconds**, including all five browser tests. Ruff, Spectral,
ShellCheck, TypeScript and DOM/recovery/archive checks passed; the locked audit
found no known vulnerabilities/adverse statuses in 153 packages. Installed-wheel
and hosted CI counts are retained separately in the handoff logs.

`scripts/qualify_recolor_workflow.py` exercises an acknowledgment failure followed
by all three successful modes through actual GUI controls. It downloads every
PDF/manifest and compares downloaded bytes with published bytes. The CLI RGB
result must render identically to the GUI RGB result. PDFium independently
extracts text and renders every source/output page at 150 and 300 DPI. Frozen
checks cover exact page dimensions and text, all-white blank truth, grayscale
channel spread of at most 2/255, retained colored-patch dominant channels, at
least 0.98 intersection-over-union of nonwhite pixels, and RGB mean channel error
at most 1/255 with at most 1% of pixels exceeding 32/255. These are synthetic
component/geometry checks, not an ICC calibration or colorimetry certification.

The capture retains 24 source/GUI-output page renders, four GUI screenshots,
the actual PDFs, manifests and measured values. CLI page renders must match the
corresponding six RGB rasters exactly. Reload retains results without resubmission;
the 390-pixel mobile layout must have no horizontal overflow.

```sh
export UV_CACHE_DIR=/workspace/.cache/uv
uv run --locked --extra html --extra ocr --with playwright pytest -q
uv run --locked --extra html --extra ocr --with playwright python \
  -m scripts.qualify_recolor_workflow --output /workspace/new-recolor-evidence
```

The `--require-installed` mode rejects importing product code from `src/` and is
used with the exact built wheel in a separate uv environment. Test-fixture and
harness code remain source-side infrastructure. Evidence is retained under
`/workspace/pdf-evidence-20261015/recolor-followup`.

## Remaining scope

Calibrated print proofing, user-supplied ICC profiles, spot colors, overprint,
annotation/form appearance conversion, conformance and selected-page editing
remain unqualified or unexposed. The entire PY-036 family is not declared complete.
Parent review at the required observed zoom levels and the source-built Poppler
26.10 comparator remain pending. Earlier Linux filesystem/cgroup, model-download,
Apple-platform and private-GUI-route limits are unchanged. The supplied
APP-ERROR-V1 contract is still outstanding; existing error strings are not claimed
to implement it. No prior corpus, oracle or evidence was rewritten.
