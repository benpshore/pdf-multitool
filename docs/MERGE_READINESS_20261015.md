# Python stack readiness follow-up — 2026-10-15

Base verified: PR #6 `24f8dce90ddf01173da2dc385378bdd4f1830b34`.
Branch: `codex/merge-readiness-20261015`. No merge, tag push, release,
deployment, protection change, model download or model-access bypass was run.

## PR3 CodeQL response-splitting thread

Thread `PRRT_kwDOVIojfM6sKO8x`, comment `4266697565`, points to the old
`send_file` Content-Type line. At the PR6 base, user display names are sanitized
and URL-quoted for Content-Disposition; internal UUID-based paths plus supported
suffixes select the MIME type. Three real HTTP upload/original-download probes
against that exact base, including CRLF, percent-encoded CRLF and Unicode/quoted
filenames, preserved PDF bytes and returned unsplit headers. No user-filename-only
exploit was reproduced; the reported dataflow appears to be a false positive.

The current change nevertheless removes the host MIME registry from the header
path entirely: Content-Type is selected from fixed supported-type strings, with
an octet-stream fallback. A real HTTP regression sends a CRLF filename while
mocking a hostile host MIME registration and asserts exact PDF MIME, no injected
header, intact body bytes and safe Content-Disposition. Static HTML/CSS/JS MIME
values are checked too. This is a targeted removal of ambiguity, not a claim that
the original alert demonstrated a remotely exploitable bug.

The old thread is left for the parent's independent review/consolidation. This
stacked PR does not trigger the existing CodeQL workflow (it targets `main` only);
no new CodeQL clean result or thread resolution is claimed.

## Release validation parity

CI, tagged release and auto-release now call the same local composite action:
`.github/actions/python-validation`. It provisions the qualified native tools
(Poppler, Ghostscript, Pandoc, Pango and fonts), including HarfBuzz-Subset for
WeasyPrint; installs locked HTML and OCR extras; runs Ruff, Python tests, bounded
offline OCR configuration smoke and the complete lock audit; builds wheel/sdist;
and exercises the installed wheel's HTML/CLI/HTTP and OCR configuration with
exported locked requirements. It never invokes model inference or publishing.

Auto-release still waits for its exact commit's `ci` check and retains its existing
guard and publishing step. Both release workflows must finish the shared action
before their existing publishing step. Required jobs/checks and permissions were
not relaxed. The shared action is exercised by required CI on this checkpoint;
release/tag publishing itself has intentionally not been executed.

## Recovery and cancellation evidence

- The mocked DOM recovery test runs separately with saved modes `clipboard`,
  `download` and `view`, counts clipboard/fallback-copy and download side effects,
  and requires zero on restore. Fresh explicit exports prove the observers work.
- Real Chromium grants clipboard permissions and checks all three saved modes.
  Before reload it sets a synthetic clipboard sentinel; restoration must leave it
  unchanged, issue no new download, preserve text and retain the same job ID.
- The deterministic cancellation child now runs the actual rotate engine before
  signaling readiness. The parent opens its produced PDF, checks visible content,
  confirms there is no result.json or published result, cancels, then requires the
  child to be reaped and the entire unfinished part directory removed. The separate
  browser renderer cancellation also asserts removal after first-PNG readiness.

## Manifest FIFO startup regression

The manifest size limit did not bound a blocking `open()` on a FIFO. Validation
now uses existing `read_local_json` (nonblocking open, regular-file fstat check,
size cap and strict JSON). The existing resolved-manifest symlink policy remains:
an alias to a regular manifest is accepted, and a FIFO reached via an alias is
rejected. Direct and symlinked FIFO startup tests run Workbench in disposable
subprocesses with 10-second timeouts so a regression cannot hang the test runner.
Other engines remain usable while Docling reports the invalid manifest.

## Validation and remaining boundaries

Reproduce local integration and the browser checks:

```sh
uv run --locked --extra html --extra ocr --with playwright pytest -q
node scripts/test_ui_reload.cjs
uv run --locked --extra ocr python scripts/smoke_docling_configuration.py
```

Ruff, all DOM scenarios, TypeScript, Spectral, ShellCheck (including extracted
workflow/composite shell blocks), YAML parsing and shared-action ordering checks
were run. Local installed-wheel commands with both HTML/OCR extras and hashed
exported locked requirements pass. Exact test counts, commit/tree identities and
hosted CI links are in the checkpoint PR/handoff.

The model metadata endpoint remains tunnel-denied (403); full Docling inference,
OCR accuracy, OS-level native network isolation, Safari and other OSes remain
unqualified. Local OSV access remains tunnel-blocked; hosted audit is required.
Independent stack review/consolidation is still the parent's responsibility.
