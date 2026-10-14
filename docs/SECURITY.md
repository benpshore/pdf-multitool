# Security design and limits

This is a single-user development tool, not a multi-tenant upload service or an
exploit-proof document sandbox. PDF parsers are complex native code. Process
limits reduce accidents and denial of service; they do not establish isolation
against a parser vulnerability. Run high-risk files in an OS sandbox or disposable
machine with no valuable data and no network.

## Implemented controls

- Loopback-only HTTP, exact Host/Origin checks, high-entropy header token, no CORS
- Static content whitelist, no path-controlled file reads, private IDs for files
- Four simultaneous HTTP connections; uploads streamed in 64 KiB chunks with
  session byte/file reservations before reading bodies
- 200 input files, 50 MiB/file, 500 MiB uploads and 500 MiB session job output;
  limits and output quotas checked during execution and after process exit.
  Quota scans stop above 100,000 directory entries and tolerate vanished native
  cache files without suppressing permission or other filesystem errors
- One shared processing gate for jobs, previews, archive imports and bundles;
  four HTTP connections cannot create four engine workers.
- 2,000 pages/input, at most 100 rendered pages/input, maximum 6,000-pixel side;
  requested DPI is honored below that cap
- Argument-array subprocesses, never shell interpolation; paths resolved before
  engine invocation; controlled generated output names
- Worker process groups, cancellation and descendant cleanup, wall-time limits,
  bounded diagnostic output, Linux CPU/address-space/file-size/open-file limits
  (512 open descriptors, sufficient for the bounded 200-image preflight)
- Private work directories and files; no password payload files or argv secrets
- Worker environment allowlist excludes unrelated credentials; completed jobs
  drop retained password/fill/creation-value references (not guaranteed memory zeroization)
- Form schemas are validated before editing; unknown keys, unsafe coordinates,
  ambiguous radio groups and unsupported signature/script fields fail explicitly.
  Saved forms are reopened and checked. Verification-only value snapshots are
  removed before change reports are written.
- JSON boundaries reject duplicate keys, nonfinite constants and depth above 64.
  CLI configuration reads require a bounded regular local file (128 KiB); API
  job bodies are bounded to 32 KiB. FIFO/device paths are not read as config.
- No originals overwritten, unique output run directories, failed partial work
  discarded, explicit partial-batch reporting
- Uploaded HTML is displayed only as inert text in the GUI. HTML-to-PDF rejects
  all resource URLs; generated repagination permits only bounded embedded images.
- No intermediate untrusted XML parser is used by the application.
- Offline model flags, explicit complete model manifest and digest checks,
  disabled Docling remote services/plugins/download option
- GUI uses textContent for names/results, validates same-origin download paths,
  downloads blobs without replacing the current app page, and clears password
  fields after successful submission
- Archive extraction never uses extractall or member-controlled filesystem paths;
  quotas include skipped members, and unsafe containers fail atomically. Output
  bundles are exclusively published only after their private temporary file is
  complete. Nested archive expansion is disabled.
- Image header and aggregate pixel preflight, strict supported formats, animation
  and layered-JPX rejection, decoder verification, orientation normalization and
  metadata stripping before new image pages are written. Native codec parsing
  has the same untrusted-document sandbox limitation as PDF parsing.
- Markdown escaping is tested through a real preinstalled CommonMark renderer,
  including styled/superscript/monospace spans and literal link/image syntax.

## Important limits

The Python audit hook rejects Python socket connection/name-resolution calls.
It is **not OS-level network isolation**: native libraries and subprocesses can
bypass Python audit hooks. No native network namespace/seccomp sandbox was proven.
Docling remains disabled without a verified model set; do not relax that gate.

The model manifest format is an operator-supplied record. Matching entries and
local SHA-256 checks do not themselves authenticate an upstream publisher or
prove that its claimed source evidence is true. Provisioning must independently
verify the upstream facts before trusting a manifest.

Windows does not receive POSIX resource limits/process-group guarantees. The
development runtime and real engine tests were Linux only. The app must not be
advertised as tested on other operating systems or Mobile Safari yet.

Text redaction matches the existing text layer. Image-only text requires rectangle
redaction. Redaction removes targeted page content plus common nonvisual data
reservoirs (including accessibility tags and catalog/page private application
data), then checks extracted page text and residual serialized string values.
Unknown residual target strings make text-redaction output fail closed. Existing
pending redaction annotations are discarded without applying them to other pages.
The tests are synthetic and do not establish universal forensic
erasure for every malformed or adversarial PDF. Never call a drawn black overlay
a successful redaction.

Automatic naming, headings, reading order and OCR eligibility are conservative
heuristics. No original-character completeness percentage is fabricated. Original
documents stay available, and uncertain names retain the original filename.
