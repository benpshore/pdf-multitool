# PDF Oxide visual review checkpoint — merge hold

The package was captured from uncommitted integration work based on
`ceee499af1bb305c906d7aee3c8b76541456e6f9`. Its code fingerprints are not a claim
that all files came from an atomic commit. Subsequent source commits preserve
the implementation and tests; this earlier evidence remains immutable.

- [20-page review PDF](https://chatgpt.com/api/library/files/libfile_c3f3677096c881919d56095644ca32d9/download), SHA256 `54bdcd79a52e96f5546abde01540882d05d56e1969db9c78aa0ef73b0afcf57f`.
- [1,450-file original/output ZIP](https://chatgpt.com/api/library/files/libfile_bad9efdff50c8191af3e0c6829cd0829/download), SHA256 `c9e9916b39a550b066155550d4e5163c8de1cd1055be05df8b46317a2458ec14`.

All 1,449 manifest entries were hash-verified before delivery. The ZIP contains
72 synthetic extraction cases with original PDFs, PDFium 72/144/288 DPI renders,
PDFium/Poppler extracted text, native outputs, frozen oracles and full comparisons.
It also preserves creation, editing, form/annotation and analysis examples,
including failed native derivatives. It contains no private user documents,
credentials, private keys, model weights or worker logs. It is not representative
real-document qualification.

All 72 extraction workers completed. Exact ordered text matched in 38 of 50
scored cases; exact table-cell matrices matched in 20 of 35 scored cases; prose
avoided false tables in 12 of 15 scored controls. The remaining cases in each
category were unscored, not accepted. Native failure outputs and expected
answers were preserved without heuristic repair.

Parent Ada reported reviewing all 20 PDF pages and independently rendering three
underlying derivatives with Poppler at 240 DPI. Ada confirmed:

- Native generated table text crosses grid borders.
- HTML/CSS table cells overlap into an unreadable cluster.
- Native text replacement paints a black mask.
- Wrapped descriptions split into extra rows; an empty-row table loses its
  header and empty row; short prose generates a false table.
- Optional cleanup removes a meaningful repeated heading and citation.
- Simple numeric and missing-cell samples appeared correct.
- Image review panels omit images, so no image fidelity was accepted.
- Unicode-filled/flattened forms visibly retained content, but appearance did
  not establish form semantics.

Ada independently verified the ZIP hash. This review confirms failures and
some narrow observations; it does not establish full parity, image fidelity,
form semantics or general correctness. Defective operations must fail clearly
or use an independently verified existing fallback with explicit provenance.
The current integration gates confirmed defective paths without replacing
native results silently. All original experiments and frozen oracles remain.

The dependency is PDF Oxide 0.3.78. Inspected Linux wheel SHA256:
`0b7e03008ca441e9183ef8f3c5d895424aca8119d5a860a205e1d16649f4f6a8`.
No merge, release, deployment or acceptance change is authorized by this record.
