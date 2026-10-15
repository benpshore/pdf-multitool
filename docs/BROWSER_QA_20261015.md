# Loopback browser QA — 2026-10-15

Base verified against published draft PR #4:
`b5cfd96e13e00e3372c67a3378b5566a62d87806`, on
`codex/python314-qualification-20261014` (stacked on PR #3).
This continuation uses `codex/browser-qa-20261015`.

## Demonstrated defect and fix

Completed jobs were removed from session recovery state, so navigating away and
Back, or reloading, lost their result cards. Retain the latest job's metadata
until a new job is started or the server rejects that ID. Recover text results
in view mode to avoid repeating automatic downloads or clipboard writes.
Document contents and passwords are not added to browser storage.

## Reproducible browser coverage

Run with system `chromium` installed:

```sh
uv run --extra html --with playwright pytest tests/test_browser.py -q -s
```

Real Chromium 151.0.7922.173, CPython 3.14.7, Linux, loopback HTTP:

- Upload a synthetic PDF with a Unicode filename; rotate and download it.
- Check downloaded bytes against the server output, the suggested filename,
  PDF rotation, and retained text.
- Open the original through the UI; compare the actual JavaScript response bytes
  with the input and verify the UTF-8 Content-Disposition filename. Chromium's
  DevTools response.body returned empty for this popup-associated fetch, so the
  test observes a cloned Fetch response without replacing the response or server.
- At 390 × 844 CSS pixels, check horizontal overflow and exercise controls.
- Repeated synchronous Run clicks create one job. Back and reload restore results.
- Inject a browser transport failure, retry against the actual server, provoke a
  real out-of-range page engine error, then correct and retry.
- Render a synthetic 100-page PDF, wait for the first actual output PNG, cancel
  through the UI (repeated click), verify cancellation and original preservation,
  then successfully run another operation.
- No uncaught browser JavaScript errors.

The browser test is opt-in and skipped without Playwright/system Chromium;
existing hosted CI does not install these tools. This is not Safari, Firefox,
physical-phone, visual/accessibility, or cross-platform qualification.

## Renderer and process regression coverage

The full production HTML renderer sees existing local stylesheet/import, font,
and both document-level and anchor attachment references. Tests assert every
sentinel URL reaches the deny fetcher, default transport is never called, public
text survives, private sentinel text and attachments do not appear in the PDF,
and input bytes are unchanged.

The supervisor cancellation test now waits for a real child to enter
`worker.process` before cancellation, instead of sleeping for a guessed interval.
A test-only child wrapper signals readiness and waits; the test verifies child
reaping, removal of unfinished output, original preservation, and successful
retry. The separate browser test cancels an actual renderer after first output.

## Validation and limits

Ruff format/check, DOM scripts, TypeScript checks/tests, Spectral API lint,
ShellCheck, wheel/sdist build, and installed-wheel CLI/HTTP/HTML smoke were run.
Combined local suite: 256 passed, 349 subtests passed (64.55 seconds), including
the real browser test. Without Playwright: 255 passed, one browser skip. The
additional final transport-not-called assertion passed in the five HTML tests.
The final handoff records the exact commit and hosted CI.
Local OSV audit is blocked by the environment's network tunnel; no advisory or
audit gate was disabled. All documents are synthetic. No model execution or
model downloads were added. Legacy pdftextract FFI checkpoints were not changed;
this does not qualify a final pure-Rust architecture.
