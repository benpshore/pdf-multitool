# Python 0.2 archive and structured-export extension

This extension uses Python's standard library and the existing preinstalled
PyMuPDF engine. No dependencies, models, toolchains or network routes were added.
The prior source checkpoint is preserved independently.

## Input pipeline

- GUI file/folder/drop intake accepts PDF, HTML, ZIP, TAR, gzip, TAR.GZ and TGZ.
- CLI document operations accept supported archives and bounded folders directly;
  `unpack` produces the supported document members without further processing.
- Compressed inputs are private copies. Extraction uses flat generated filenames
  and keeps original member paths as display metadata, never as filesystem paths.
- All members are validated and drained, including unsupported/nested members;
  skipped ZIP members still undergo CRC/size validation.
- Explicit bounds cover compressed/expanded/member bytes, archive entries,
  document count, path depth, expansion ratio and elapsed time.
- Duplicate and Unicode/case-normalized collisions, traversal, absolute/drive/
  UNC paths, symlinks, hardlinks, devices, sparse TAR entries and encrypted ZIP
  entries are rejected. Nested archives are not recursively unpacked.
- Failed imports remove partial members and reservations. The source archive
  remains downloadable while any imported member remains in the workspace.
- Import, preview and document/bundle execution share one processing gate.
  Upload transport remains separately bounded to four HTTP connections.

## Exports

`markdown` writes actual headings, page navigation, source-page labels, escaped
text/style/script content and safe companion PNG/JPEG image files. `textlayout`
writes UTF-8 text using the same conservative geometry and paragraph decisions.
`pdfhtml` retains its self-contained image reader and explicit source-page links.

Optional repeated-margin omission is off by default and reports omitted
line/character counts. Formula-like operator lines, monospace lines and uncertain
hyphen endings are not blindly joined or rewritten. Source counts are parser
observations, not proof of original-character completeness.

Packaging uses only completed output records. Primary files retain their public
download names, and image directories retain their relative paths. Each source
document receives a separate archive folder. Output archives are completed in a
private temporary file and published exclusively, without overwriting an existing
destination. File ownership, original timestamps and external symlink targets
are not copied into bundles.

## Verification and limits

Synthetic tests cover the archive parser's malicious/invalid boundaries and real
HTTP/CLI archive→native-operation→download paths. Markdown image paths are checked
against actual ZIP members. Preinstalled Pandoc's CommonMark parser is used only
as a development-test renderer for escaping; it is not a new runtime dependency
or an engine substitute. Browser visual/Safari tests remain unavailable.

ZIP intake deliberately supports stored and deflate methods; unsupported codecs
and ZIP64 directories fail closed. TAR overhead consumes the expanded-byte quota.
Output packaging supports ZIP and TAR.GZ. The workspace is still a single-user
loopback development tool, with no hostile-document OS sandbox or hosted service.
OCR, Docling, WeasyPrint/real re-pagination and the release-provenance gates remain
not yet complete; see provenance.md.
