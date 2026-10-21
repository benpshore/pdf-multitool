# Dependency security qualification — 2026-10-21

## Disposition: blocked, partial correction only

Baseline: PR #14 at `60d79e194a6c3cfedd5e26234b3b5433b60b46e4`.
This correction is stacked on that PR. No merge/release is authorized or performed.
The mandatory audit remains enabled and failing. No advisory ignores, dependency
metadata overrides, platform exclusions, or removal of OCR functionality were used.

Changes:

- Regenerate the universal lock with uv 0.12.23, updating only certifi to
  2026.10.19 and restoring PyMuPDF to 1.26.7. uv advances lock revision 3 to 5.
- Withdraw PR #14's PyMuPDF 1.28.2 upgrade after reproducing a rectangle-redaction
  leak. Keep ReportLab 5.0.1, whose existing form fixtures pass.
- Add a synthetic regression test and standalone qualification probe.

## Independently checked advisory evidence

Original job: https://github.com/benpshore/pdf-multitool/actions/runs/39531984157/job/118999772067

The original audit has 11 entries, but its two certifi entries use the same
PYSEC identifier and GHSA. Treat them as one advisory, not two distinct flaws.

Certifi primary advisory:
https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2

The advisory identifies compromised intermediate CA keys and lists 2026.10.18
as patched; the audit lists 2026.10.19. This discrepancy is recorded rather than
silently reconciled. The published 2026.10.19 package resolves with all existing
extras, and the audit no longer reports certifi. An SSLContext loaded its 117
roots. Comparison with 2026.7.22 showed removal of `emSign ECC Root CA - G3`;
three other emSign roots remain. Therefore a blanket assertion that every
emSign root is removed would be incorrect. No live MITM/TLS exploit test was run.

Release: https://github.com/certifi/python-certifi/releases/tag/2026.10.19

Pillow's own 13.0.0 release notes identify all nine reported GHSA IDs as fixes:
https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html

| GHSA | Reported issue | Direct image-import exposure assessment |
| --- | --- | --- |
| 65qm-jvmh-6pqg | GIF seek size after bomb rejection | GIF excluded |
| vx6w-vv9f-6gvg | EPS scale bypass | EPS excluded |
| 58r4-6rv3-gfh8 | Zero-width morphology | No ImageMorph call found |
| q45j-58j2-45rg | Short JPEG strips in TIFF | TIFF excluded |
| 462x-pgg6-55fp | TIFF tile buffer read | TIFF excluded |
| 8g5g-8vmr-9782 | JPEG decode heap overflow | JPEG accepted; direct path uses load, not draft/thumbnail |
| c6hw-5grf-ff96 | TIFF pixel disclosure | TIFF excluded |
| p4wg-p73j-c528 | Pillow PDF parser recursion | PDFs processed by PyMuPDF/Docling, not Pillow PdfParser directly |
| pchr-8j38-3x4p | TIFF colormap encoder read | No direct TIFF encoding path |

This is source reachability triage, not an exemption. Indirect OCR, HTML and
third-party image paths are not exhaustively assessed. Existing preflight,
timeouts and size bounds do not prove memory safety. The JPEG advisory explicitly
warns that JPEG-only format filtering is insufficient when draft scaling is used:
https://github.com/python-pillow/Pillow/security/advisories/GHSA-8g5g-8vmr-9782

The release notes also contain additional fixes beyond the audit's nine entries,
including OpenJPEG and Exif issues relevant enough for further triage. A clean
version database would not establish complete application security.

## Pillow 13 graph blocker

Both development and images pins must change together. Even after doing so,
Docling 2.138.0 -> docling-slim[standard] 2.138.0 requires Pillow <13.
A separate resolver experiment permitted newer Docling versions; all published
versions through 2.141.0 retain that restriction. PyPI metadata independently
confirms it. There is no published 12.x version newer than 12.3.0 in the checked
index. See `pillow13-resolution.txt` for the resolver evidence.

Do not force Pillow 13 using uv override-dependencies: that would violate the
upstream contract, and isolated tests cannot establish OCR compatibility.
The supported next step is an upstream compatible release (or a separately
maintained, fully qualified patch to upstream metadata/code). Splitting OCR into
another environment without fixing its Pillow would merely relocate the finding.

## Compatibility results

Pillow 13: 124 focused tests passed in an isolated environment with PyMuPDF
1.28.2, ReportLab 5.0.1 and pytest. This covered all existing image, image workflow,
form, cross-page form and document-operation tests. It excludes OCR and precedes
the new redaction regression. It is not validation of the complete dependency graph.
Pillow 13 removes saving mode I directly as PNG; existing high-depth fixtures
already use I;16. No removed API use was found in the direct image module.

ReportLab: development-only fixture generator. Primary 5.0 notes change remote
image trust defaults and remove legacy renderPM/pyRXP backends; 5.0.1 also checks
redirected image hosts. No use of those removed backends or remote fixture images
was found. Existing radio-group/form tests passed without relaxing security settings.
Sources: https://docs.reportlab.com/releases/notes/whats-new-50/
and https://hg.reportlab.com/hg-public/reportlab/log/default

PyMuPDF: upstream issue #5152 describes incorrect handling of TL + Td + the quote
text operator. A separately generated synthetic fixture reproduces the defect at
the application boundary: text-based redaction fails closed, but rectangle-only
redaction completes and exposes the target text. On 1.26.7 both operations succeed
and remove it. `pymupdf1282-regression.txt` records the new test's failing subtests;
`redaction_probe.py` reports leakage and exits nonzero. The pin rollback prevents
introducing this demonstrated regression; it does not certify the old engine.

https://github.com/pymupdf/PyMuPDF/issues/5152
https://pymupdf.readthedocs.io/en/latest/changes.html

Other upstream reports needing separate qualification include #5055/#5165 radio
widgets, #5082/#5125 rendering/extraction hangs, #5101 cyclic widget parents,
#5112 annotation callout memory safety, and CVE-2026-82035 CLI font extraction.
The latter two are not claimed reachable through this application's interface.
Jumping to PyMuPDF 2.0 would introduce a separate major-version qualification task.

## Final local gates

Fresh `.venv-qualified` installed from the complete locked HTML/OCR graph on
Linux x86_64, Python 3.14.8. Import checks: PyMuPDF 1.26.7, Pillow 12.3.0,
certifi 2026.10.19. A fresh environment was used after a preliminary environment
reported stale package metadata; preliminary mixed-environment results are not
used for final acceptance.

| Gate | Result |
| --- | --- |
| Universal locked resolution and sync with HTML/OCR | Passed, 150 packages resolved |
| Ruff formatting and lint | Passed |
| Full regression suite, including new test | 393 passed, 1 skipped |
| Malformed-input, forms, images, transformations | Included in full suite |
| Locked dependency audit | FAILED: nine Pillow entries, zero certifi entries |
| Wheel and sdist build | Passed |
| Isolated installed-wheel CLI/HTTP and HTML smoke | Passed |
| Installed-wheel OCR configuration smoke | Passed; no model inference/downloads |
| Spectral, warning-level threshold | Passed |
| ShellCheck (via uvx shellcheck-py) | Passed |
| TypeScript typecheck and tests | Passed; one test |

Browser test skipped because Playwright/system Chromium are unavailable.
One warning reports missing HarfBuzz-Subset for future WeasyPrint versions.
macOS/Windows native behavior, live OCR inference, advisory exploit corpus,
long-running memory behavior and the upstream issues above remain unqualified.
CI and independent review remain mandatory. The previous PR's CodeQL success
must not be treated as qualification of this correction.
