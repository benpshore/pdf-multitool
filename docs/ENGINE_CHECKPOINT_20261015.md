# Python engine checkpoint — 2026-10-15

Work ran only in the saved Linux cloud workspace, `/workspace/pdf-multitool`,
using Python 3.14.8. No Mac, merge, release, deployment, new PR, license change,
permission change, credential change or private-code import was used.

The checkpoint branch is `codex/python-engine-completion-20261015`, based on
PR 12's preserved evidence head `7863d37e6f47862fee8f8c084a838af12e998e37`.
Implementation commit `4173accf66e4908a62489ae7523b70e7ba6ef897` was pushed and
confirmed with `git ls-remote`. The next checkpoint adds CI native dependencies,
consistent number/password control styling and this report. PR 12 remains held;
its head was not moved. There is no new PR.

## Implemented and exercised

Eleven additional operation contracts support text PDF creation, headers/footers,
pdfplumber tables/geometry, pikepdf lossless optimization/inspection/attachments,
object deduplication, native rendering, Tesseract OCR and planar deskew. Shared
contracts drive validation, CLI options and actual browser controls. The OpenAPI
document now describes the real HTTP routes. Existing operations remain intact.

Linux native socket restrictions survive helper exec. Process-group escape
syscalls are denied, UID task headroom is bounded, stdin delivery obeys deadlines,
version queries have bounded capture, and result JSON/symlinks are validated.
These are tested controls, not a native filesystem sandbox claim.

| Evidence | Exact result |
| --- | --- |
| Preserved baseline on PR 12 | 392 passed, 1 browser skip, 371 subtests; 60.02 seconds |
| New functional + isolation suite | 46 passed; 11.70 seconds |
| Existing browser + isolation after task-ceiling correction | 7 passed, 3 subtests; 18.56 seconds |
| Full suite with real Chromium | 443 passed, 374 subtests; 119.39 seconds |
| Subsequently added full-stack browser wrapper test | 1 passed; 22.46 seconds |
| Final visual capture after control styling | 12 actual browser scenarios, 13 PDF pages, 26 page renders, 14 screenshots |
| Locked dependency audit | No known vulnerabilities/adverse statuses in 153 packages |
| Ruff format/check, Spectral, ShellCheck, TypeScript check/test, DOM scenarios | Passed locally |
| Wheel/sdist build and isolated installed-wheel CLI/HTTP/HTML smoke | Passed |
| Offline Docling construction smoke | Passed with downloads/remote services/plugins disabled; **no inference** |
| Hosted required `ci` | Not established by these local results; still required before integration |

The 443-test run and the subsequently added browser wrapper are separate runs;
they are not described as a single 444-test invocation. The latter invokes the
same twelve-scenario visual harness. Screenshots were inspected for creation,
mobile layout, Unicode output and deskew, but the parent's requested independent
review has not occurred. Synthetic tests do not establish comprehensive product
acceptance or conformance to the entire named feature inventory.

Tests caught and corrected a reused subset-font resource in header insertion,
an invalid textarea property assignment, and a process limit that initially
counted Chromium's existing threads against a fixed ceiling. The visual harness
also corrected its own reload assertion: rasterization acknowledgment resets on
reload while completed results recover without resubmission. Failed initial
capture logs/directories remain in the cloud evidence directory.

## Preservation and inspection artifacts

`/workspace/pdf-evidence-20261015/before.bundle` preserves all starting Git refs;
`source-before.json` contains SHA-256 for all 467 starting tracked files.
All **378 files under the existing experiments tree** were checked byte-for-byte
unchanged. No frozen oracle, old observation, fixture or source file was deleted.

The final independently inspectable pack is
`/workspace/pdf-evidence-20261015/inspection-pack.tar.gz` (1,926,469 bytes), SHA-256:

`39d909daa5afba2d52052c9f291e9a95e6056175332b84323d8935acdae6e362`

It contains synthetic originals, every published workflow output, screenshots,
page-by-page 72/144-DPI renders, `report.json` with exact versions/options/page
inventory, per-file `sha256.json`, and test evidence. Private runtime caches and
launch tokens are excluded. The retained unpacked source capture is
`/workspace/pdf-evidence-20261015/visual-run4`.

Reproduce on the same cloud host from this checkout:

```sh
export UV_CACHE_DIR=/workspace/.cache/uv
uv sync --locked --extra html --extra ocr
uv run --locked --extra html --extra ocr --with playwright pytest -q
uv run --locked --extra html --extra ocr --with playwright python \
  -m scripts.qualify_engine_workflows --output /workspace/new-inspection-directory
uv run --locked --extra html --extra ocr pdf-multitool gui --no-browser --port 8765
```

The final command prints a private loopback launch URL. Open it in the same cloud
environment; do not tunnel it publicly. Upload the pack's `fixtures/*.pdf` for
manual review. OCR/deskew need the rasterization checkbox; successful output is
downloaded from the result controls. Cancellation and reload recovery are covered
by real Chromium tests. System dependencies include Poppler, Ghostscript,
Tesseract English data, libseccomp, Pango/HarfBuzz, fonts and Chromium for browser
qualification. CI's shared native provisioning now includes Tesseract/libseccomp.

## Actual blockers and unfinished scope

- The complete official matrix v2 archive was authorized by Library, but its
  signed transfer failed with `Tunnel connection failed: 403 Forbidden`. Library
  text-read returned no readable content. Its bytes and supplied hash were not
  verified. The claimed 198 families / 1,626 proposed cases are not mapped or
  reported as executed.
- Official Poppler 26.10 source download returned HTTP 403. This host's
  `pdftoppm` reports 26.05.0 while `pdftotext` reports 25.03.0. There is no
  source-built 26.10 comparison.
- Bubblewrap namespace setup was denied; Landlock returned ENOSYS. Native
  filesystem isolation remains unqualified.
- Docling model inference remains gated; curved-page dewarping, media codecs/ASR,
  Apple Vision, PKI signatures, full toolkit parity and standards conformance
  remain unimplemented or unqualified. See [the scope ledger](TOOLKIT_FEATURE_MATRIX.md).
- The cloud command transport briefly disconnected while packaging, then resumed
  in the same workspace. Remote readback and artifact hashes were checked after
  recovery; no cancellation or restart was inferred.

This is a durable, tested **partial implementation checkpoint**, not completion
against the full requested inventory. The next work is matrix reconciliation,
independent visual review and the remaining engine/sandbox qualifications.
