# Contract and evidence qualification

The acceptance scope in ACCEPTANCE.md was committed before implementation at
`a23361e90964789f8d3060911609d753c5e55bea`. Its chosen contract is horizontal
LTR spatial reading for adjacent alphabetic words. It is not a claim that this
order is the only semantics encoded by a PDF.

In the synthetic reversed-object example, content-stream order is BETA then
ALPHA, while ALPHA is geometrically left of BETA. There is no tagged reading
order. Both facts are retained in the real-engine test: it asserts BETA-first
raw parser order before export and unchanged raw extraction afterward, while
requiring ALPHA-first exported order under the chosen contract. The original
PDF is not rewritten. The repair orders two parser lines locally; it does not
rewrite spans or establish document-wide semantic order.

PR8's expectations were frozen independently of the measured outputs, but
independent human semantic adjudication is not established. Its table/prose
expectations are author-declared; sparse geometry alone underdetermines them.
Closely adjacent alphabetic cell labels can meet this repair's gates. Numeric
and wider cells are excluded; general table inference remains unresolved.

At implementation checkpoint `76a9e2e9bf6c9aa73c3f3b053f1efe3fcf29f505`:

- 43 focused tests passed, including real-engine pixel equivalence at 72/144
  DPI, three export formats, negative controls and repeated Form occurrences.
- Local CPython 3.14.7: 307 tests and 371 subtests passed. The optional browser
  test skipped in that run and separately passed using Chromium, with 3 subtests.
- Read-only comparison with PR8 checkpoint
  `d6dbcc6e4bbbef082ef80c475bdb2ce2fe4ba595` found changes only in the three
  objects_reverse exports; the other 30 exports were byte-identical. No old
  fixtures, expectations, measurements or measurement guards were modified.
- Hosted CI run 38688806160 passed, including the complete locked audit and
  installed-wheel smoke checks. Local OSV access was blocked by the proxy.

All input PDFs were synthetic. These observations are for PyMuPDF 1.26.7 /
MuPDF 1.26.12, not proof across PDF engines or independent semantic adjudication.
