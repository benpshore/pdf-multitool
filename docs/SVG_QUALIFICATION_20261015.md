# SVG engine follow-up — 15 October 2026

This checkpoint adds the supplied inventory's PY-006 SVG page-export route to the
Python engine, CLI, loopback API and real GUI. It follows commit
`662a5e449d5636adcd1068ec8a3863a3357c607c` in the saved Linux cloud environment.
The existing PR 12 remains under a merge hold. This is bounded implementation and
synthetic evidence, not full feature acceptance.

## Received contract and reconciliation

The parent supplied readable `feature_contracts.json` and `ACCEPTANCE.md` through
native Library text access. The assembled UTF-8 files parsed successfully and
matched the reported byte sizes:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| feature_contracts.json | 274,364 | `b4aeb7325765cceabe3d1164119b67036d1a18e8f9b8746b01a20cf625ebdfe5` |
| ACCEPTANCE.md | 34,732 | `eec52cb604c777de5d0e3ff92384ba4f73ba3ab0d944de86061378afd330914f` |

The [reconciliation](feature_reconciliation.json) covers all 198 families and
464 referenced public APIs. It distinguishes exposed routes, implicit behavior,
different-provider implementations, missing functionality and platform blocks.
It does not enumerate every public symbol or claim the proposed 1,626 cases passed.
The separate case-ID dataset and API-surface requirements file have not been
received as readable files. The original proposed contracts remain unchanged.

There are version differences: the supplied matrix references PyMuPDF 1.28.2 and
Docling 2.139.0, while the app retains its pinned 1.26.7 and 2.138.0. Its linked
MuPDF and Ghostscript release/documentation versions also need reconciliation.
No dependency was silently upgraded or relabelled to close those gaps.

## Implemented behavior and bounds

`pdf-multitool svg input.pdf --pages '1-3' --text-mode paths --scale 1` creates
one SVG per selected page and a JSON manifest. The same `svg` contract supplies
API validation and actual GUI controls. Uniform scale is 0.25–4; the visible
CropBox and rotation determine output geometry. `paths` outlines glyphs using
the pinned engine; `text` preserves text elements but requires matching viewer
fonts and warns that missing fonts may be substituted. PDF links, forms,
attachments and cryptographic signatures are not transferred.

The implementation uses the installed
[Page.get_svg_image API](https://pymupdf.readthedocs.io/en/latest/page.html#Page.get_svg_image).
It adds no dependency or engine-version change. Output is limited to 100 selected
pages, a 14,400-point side, 16 MiB and 100,000 XML elements per page. Embedded
PNG/JPEG headers are checked: 16,000-pixel side, 25 million pixels per SVG and
200 million pixels per job. Existing time, memory, file-size, cancellation,
network/process and total output limits still apply. An input's outputs are
published atomically after validation; failed attempts do not expose partial files.

The validator rejects DTDs/entities, active/foreign elements, event attributes and
external resource references in engine-generated SVG. It is not an SVG-upload
sanitizer. Downloads retain token checks, attachment disposition, `nosniff` and
the restrictive CSP. The product does not render SVG as executable page content.
Native filesystem isolation remains unavailable on this host.

## Reproducible evidence

`tests/test_svg.py` exercises actual supervised exports, both text modes, scale
boundaries, selected pages, crop/rotation, blank pages, Unicode text, embedded
images, passwords, quota failures, recovery and active/oversized output rejection.
HTTP tests exercise strict option rejection and actual SVG download headers/auth.
The first combined targeted run passed **32 tests in 16.42 seconds**. Subsequent
full-suite validation passed **474 tests and 374 subtests in 151.10 seconds**,
including all four real-browser tests. Ruff formatting/checks, Spectral,
ShellCheck, TypeScript and the four DOM/recovery/archive scripts passed. The
locked dependency audit found no known vulnerabilities or adverse project
statuses in 153 packages. Installed-distribution and hosted CI results are
recorded separately in the handoff evidence.

`scripts/qualify_svg_workflow.py` runs the CLI and both GUI modes, downloads every
result and checks byte identity with the published artifacts. CLI page 2 must
exactly match the GUI's corresponding SVG. Chromium decodes every SVG with
network requests blocked. PDFium independently renders the source PDF at matching
pixel dimensions; every page has 150- and 300-DPI SVG/PDF comparison images.
The synthetic PDF has known red/blue vector rectangles, a green raster image,
Latin/Greek/Cyrillic/CJK text, a cropped/rotated page and a declared blank page.

Frozen checks include exact interior color patches, page dimensions, blank truth,
localized Unicode ink and bounded whole-page differences for glyph paths. The
path-mode thresholds are mean channel error at most 3/255, at most 2% of pixels
with a channel difference above 64, and glyph-region ink ratio 0.8–1.25. Text-mode
font fidelity is explicitly not asserted. Reports retain all measured values,
engine versions, source/output hashes and browser errors. Three screenshots cover
both desktop results and the 390-pixel mobile layout. Reload must retain results
without creating another job.

```sh
export UV_CACHE_DIR=/workspace/.cache/uv
uv run --locked --extra html --extra ocr --with playwright pytest -q
uv run --locked --extra html --extra ocr --with playwright python \
  -m scripts.qualify_svg_workflow --output /workspace/new-svg-evidence
```

The evidence root is `/workspace/pdf-evidence-20261015/feature-followup`. The
`--require-installed` variant rejects importing the product from `src/`; it is
used with the separately built wheel in an isolated uv environment. The fixture
generator and qualification harness remain source-side test infrastructure.

## Outstanding acceptance work

- Parent visual acceptance, bound to exact artifact hashes, remains `not_reviewed`.
  Automated image comparisons do not satisfy the requested observed 100/200/400%
  review. PDFium is an explicit independent comparator; it does not substitute for
  the requested source-built Poppler 26.10 comparison. Official Poppler source and
  GitLab routes returned HTTP 403 in the retained platform probes.
- Native filesystem/cgroup containment remains blocked on this Linux host:
  Bubblewrap UID-map setup is denied, Landlock returns ENOSYS, and cgroup controls
  are not writable. Existing guards were retained without claiming this isolation.
- Complete verified Docling models remain unavailable; real inference is unqualified.
  Apple Vision/Neural Engine cannot run on this Linux host. No Mac was accessed.
- Full public-API/option enumeration, every contract variant and the supplied
  APP-ERROR-V1 schema remain unfinished. Existing error strings have not been
  misrepresented as that new schema. See the 198-row reconciliation for other gaps.
- No authenticated user-accessible private GUI route is exposed by this environment.
  Real loopback browser testing is available; no public tunnel was created.
