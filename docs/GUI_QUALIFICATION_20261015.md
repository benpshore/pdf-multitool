# GUI qualification follow-up — 15 October 2026

This follows the partial [engine checkpoint](ENGINE_CHECKPOINT_20261015.md).
All work ran in the saved Linux cloud environment on Python 3.14.8. The complete
198-family / 1,626-proposed-case inventory has not been received or qualified;
these figures are not executed test counts. PR 12 remains the existing integration
carrier with a merge hold. No new PR, merge, release or public deployment is authorized
by these results.

## Changes and observed behavior

The controls use text and CSS decorative marks instead of font-dependent arrow
and document symbols. Secondary text is at least 14 CSS pixels, form text is
16 pixels, and buttons have larger targets. Narrow layouts wrap action controls
and result names. Helper text now points to the available Tesseract and deskew tools.

The output heading names the operation that produced the displayed files. Selecting
OCR after deskew leaves the heading **Deskew scanned pages results** and explains
that OCR must be run to create new results. Reload recovery keeps this provenance.
Packaging retains the original operation in the results heading.

The real-browser usability harness creates one PDF by keyboard, selects OCR without
running it, and recovers the original result across reload and mobile contexts.
It asserts visible keyboard outlines, reachable controls, labelled visible inputs,
minimum text sizes, absence of horizontal overflow, and no accidental resubmission.
The original synthetic input's SHA-256 remains unchanged.

| Qualification | Observed result |
| --- | --- |
| Full local Python suite, including real Chromium workflows | **445 passed, 374 subtests passed in 146.04 seconds** |
| Native Chromium Appearance setting | 200%; CSS layout width 1440 → 720, devicePixelRatio 1 → 2, CSS zoom remains 1 |
| Mobile emulation | 390 and 320 CSS pixels; light and dark themes; no horizontal overflow |
| Keyboard focus | 3-pixel visible outlines; one Enter activation creates exactly one job |
| Additional engine workflow capture | 12 successful browser scenarios, 13 PDF pages, 26 page renders, 14 screenshots |
| Separate usability capture | 9 screenshots and a JSON measurement report |
| Ruff, TypeScript, DOM/reload/recovery/archive checks | Passed |
| Spectral and ShellCheck | Passed |
| Locked dependency audit | No known vulnerabilities/adverse statuses in 153 packages |
| Existing evidence preservation | All 467 starting tracked paths remain; all 378 original experiment files have unchanged hashes |

The full suite includes both browser wrappers in this invocation. These are
synthetic qualification results, not a general accessibility certification or
full product acceptance. Parent inspection of the preceding 13-page render set
was a bounded visual review. No screen-reader test or new independent human
acceptance is claimed.

Native zoom screenshots use Chromium's visible-surface capture because its
full-page capture clipped the image at native zoom. The zoom is set using the
browser's own Appearance control in a disposable profile, not CSS zoom or pinch
simulation. Initial extension-based qualification attempts and a blocked browser
download are retained in the local evidence directory; they are not counted as passes.

## Reproduction and evidence

```sh
export UV_CACHE_DIR=/workspace/.cache/uv
uv run --locked --extra html --extra ocr --with playwright pytest -q
uv run --locked --extra html --extra ocr --with playwright python \
  -m scripts.qualify_gui_usability --output /workspace/new-usability-capture
uv run --locked --extra html --extra ocr --with playwright python \
  -m scripts.qualify_engine_workflows --output /workspace/new-workflow-capture
```

The retained evidence root is `/workspace/pdf-evidence-20261015/ui-followup`.
`run5` contains the usability capture; `workflows` is the full-suite browser
capture. `full-pytest.txt`, `checks.json`, `audit.txt`, `spectral.txt`,
`shellcheck.txt`, `preservation.json`, and `qualification-blockers.json` record
the corresponding results. Earlier captures and the original inspection pack remain.

## Remaining qualifications and environmental limits

- Library authorizes the v2 matrix archive, but its signed download host returns
  HTTP 403. Archive bytes and the supplied hash remain unverified. A native Library
  Markdown/JSON version can be read through the text connector; its identifier has
  been requested. No content from a separate private repository was imported.
- [Poppler's official release page](https://poppler.freedesktop.org/) lists 26.10.0.
  Both its source tarball host and official GitLab Git route return HTTP 403 here.
  The linked GitHub mirror did not expose a 26.10 release tag, and Debian's available
  source listing stopped at 26.07.0. No 26.10 source build is claimed.
- Bubblewrap fails with `setting up uid map: Read-only file system`; Landlock's
  version query returns ENOSYS. Cgroup membership and controller files are not
  writable by UID 1000. Existing tested resource, native-network and process-group
  guards remain; they do not establish native filesystem isolation.
- The available cloud tools expose no authenticated private port-forward/GUI route.
  The actual server remains loopback-only with its Host, Origin and token checks.
  Automated cloud-browser inspection works; no user-accessible private URL or
  public tunnel was created.
- Hosted CI must be read at the new integration head before claiming it passed.
  Local checks and the older PR 12 CI run do not qualify the new commit.
- Real Docling inference, curved dewarping, media codecs/ASR, Apple Vision, PKI
  signatures and complete toolkit parity remain unqualified or unimplemented as
  recorded in the [feature ledger](TOOLKIT_FEATURE_MATRIX.md).
