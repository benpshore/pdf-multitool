# Python 3.14 qualification

Work starts from PR #3 (`8fd657da67d57c9107b42cbbdabf1bfab0ae1dfc`).
Its first `uv sync --locked --dry-run` reproduced the stale-lock failure. Normal
resolution now covers the declared core, optional and development dependencies;
neither `--frozen` nor dependency omission is used. The final core pin is
PyMuPDF 1.26.7, updated from 1.26.6 after the required audit found the advisory
below. Pillow 12.3.0, WeasyPrint 70.0 and Docling 2.138.0 remain unchanged.

The development group includes Pillow (image tests import it) and ReportLab
(synthetic form fixtures import it). The lock selects ReportLab 4.5.1. These are
test dependencies, not additional mandatory application engines. The configured
index is official PyPI. Optional OCR dependencies are resolved but have not been
installed or qualified; resolution alone does not prove runtime compatibility.

## Environment and provenance

The local interpreter is uv-managed CPython 3.14.7, Linux x86-64. Its download
catalog could not provide 3.14.8. Hosted CI uses CPython 3.14.8; local results must
not be reported as tests of that patch version.

Before installation, the repository verifier compared each selected wheel's
exact filename, HTTPS URL, size and SHA-256 in PyPI's release JSON and Simple
JSON disclosures. Downloaded bytes then matched the declared size and SHA-256.
All selected core, image, HTML and development transitive wheels received the
same check. This is agreement from one publisher authority, not independent
trust roots, a security audit or a verified Docling model set.

The [22-wheel verification record](evidence/python314-wheels.json) records public
URLs, filenames, byte counts and digests; each entry also matches `uv.lock`. Raw
metadata and wheels remain local artifacts. Build-toolchain provisioning is
separate from this runtime/test-wheel verification. The selected PyMuPDF patch
artifact is `pymupdf-1.26.7-cp310-abi3-manylinux_2_28_x86_64.whl`, SHA-256
`69dfc78f206a96e5b3ac22741263ebab945fdf51f0dbe7c5757c3511b23d9d72`.

Poppler, Ghostscript and Pandoc are preinstalled locally and installed from
Ubuntu repositories in CI. Their upstream artifact provenance was not
independently verified in this qualification. No model downloads are enabled.

## Failures reproduced and corrected

1. The stale lock prevented installation. The first unlocked development test
   run then failed collection because ReportLab was undeclared. Both dependencies
   and the lock are now explicit; no tests were skipped to hide that failure.
2. The imported sources had 560 Ruff findings. Formatting, import cleanup,
   explicit immutable defaults, strict paired iteration and targeted fixes now
   pass the existing rules. Reviewed subprocess and fixture false positives have
   local explanations; the rule selection remains intact. Source-only checks
   use the declared Python 3.14 toolchain.
3. The first hosted Python 3.14.8 run passed locked sync and Ruff but lacked
   Poppler: two native tests failed, 240 passed and six skipped. CI now provisions
   Poppler, Ghostscript, Pandoc, Pango and fonts; the corrected run passed all
   252 tests, including the new real HTML checks.
4. Installed-wheel HTML conversion initially failed with `AttributeError:
   'function' object has no attribute '_fail_on_errors'`. The pinned WeasyPrint 70
   source requires `URLFetcher`/`URLFetcherResponse`. Both replacement subclasses
   avoid default file/network handlers. User HTML denies all resource URLs;
   repagination only permits bounded generated PNG/JPEG data URLs. The size
   check precedes regex matching and decoding. Four regressions cover resource
   denial, the response API, rendered images and original preservation.
5. Hosted audit at `0a7ae34` reported CVE-2026-3029 against PyMuPDF 1.26.6,
   blocking build/smoke in that run. The [upstream change log](https://pymupdf.readthedocs.io/en/latest/changes.html)
   describes safer default attachment extraction in 1.26.7. A synthetic dependency
   regression failed all three adversarial subtests on 1.26.6 (parent traversal,
   absolute path and overwrite), while its benign extraction control passed.
   Verified 1.26.7 passes both tests and all three adversarial subtests. The app
   does not invoke PyMuPDF's `embed-extract` CLI; this is a dependency regression,
   not a claim that the app exposed that command. No advisory is ignored.

## Validation evidence and limits

- At checkpoint `0a7ae34`, the complete local suite passed 252 tests and 346
  subtests on CPython 3.14.7; hosted CPython 3.14.8 passed 252 tests. After the
  dependency patch, all 254 local tests and 349 subtests pass (54.16 seconds),
  including the two extraction-safety tests described above.
- Ruff, Python compilation, 13 artifact-verifier tests, six mocked UI/API
  scenarios, TypeScript typecheck/runtime test and OpenAPI lint pass.
- Wheel and sdist build pass. `uv build` builds its wheel from the sdist.
- The isolated installed-wheel smoke verifies the console entry point, merge
  content/page count, real HTML conversion with resource denial, packaged static
  assets, HTTP authentication/origin checks, upload/rotate/download, preservation
  of both originals and clean shutdown. It imports from site-packages and runs
  commands outside the checkout; it is not a source-tree import smoke.
- Local audit cannot reach `api.osv.dev` through the environment tunnel. Local
  shellcheck is absent; hosted shellcheck passes. Required CI still enforces audit
  and every other job. Check the [draft PR's exact-head checks](https://github.com/benpshore/pdf-multitool/pull/4/checks)
  for the final patched dependency and artifact-smoke results.
- Real-browser/Safari QA, Docling conversion with a verified model set, other
  operating systems and full product acceptance remain unverified. Synthetic
  HTML checks do not establish universal layout fidelity or OS network isolation.

No tag, release, deployment or main-branch merge is part of this qualification.
