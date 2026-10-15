# Independent frozen table-versus-prose rerun — October 15, 2026

## Finding and one recommendation

**The original borderless-table failure persists on current Python main. Adopt an explicit limitation; do not ship an automatic row-order heuristic from these examples.**

Proposed precise limitation: “Reading order is inferred. Borderless tables may export column by column; row/cell relationships are not preserved or verified. Review the source before using exported text as paired records.” This report qualifies that limitation; it does not implement a new warning or change production behavior. Existing export warnings already say table semantics are not reconstructed or verified.

## Pinned scope and preserved evidence

- Current main, verified again against GitHub after capture: `fb146a976ab1dce742a1960ef46f1165dfca731d` (merged PR #11).
- Original PR #8 checkpoint: `d6dbcc6e4bbbef082ef80c475bdb2ce2fe4ba595`; original premeasurement freeze: `ac6a1d1fedbd60f5e34d6fe9f7a8ca01af46d211`.
- All 94 original experiment files were copied byte-for-byte, including the 11 PDFs, expectations, generator, harness and 78 historical observations. Whole-tree hashes and original generator `--check` pass before and after capture.
- Production `src`, `pyproject.toml` and `uv.lock` are unchanged. The imported analysis path must be this checkout's source file and its bytes must equal the pinned git object. Its SHA-256 is `cf5b35c7f656305c6fc6ff383627ff790db9312e54e2707eb239af9d090e72e3`.
- Runtime: CPython 3.14.8, PyMuPDF 1.26.7, MuPDF 1.26.12, macOS 27.0.1 arm64. Detailed runtime, source, harness and lock hashes are in each run summary.
- The original harness correctly refuses production drift from its historical base. It remains unchanged. `capture.py` is an explicit separately identified adapter replacing that one historical guard with current-main source/dependency checks. `PRECAPTURE.json` hashes its final protocol and original-tree manifest before final measurement.
- Initial captures were retained separately under local `work/initial-captures` before formatting and strengthening import provenance after review. No expectation changed. The final published run1/run2 use the reviewed adapter; the scorer's exact historical-failure check was strengthened after review. Scorer implementation is not claimed as an independently published premeasurement freeze. Contracts were recorded before the first current-main capture.

## Results across parser stages and exports

| Case | Unsorted parser | Split blocks | Ordered blocks; text, Markdown and HTML content | Frozen semantic oracle |
| --- | --- | --- | --- | --- |
| Table, row-emitted | `A1 B1 A2 B2 A3 B3`; 3 blocks/6 lines | 6 blocks, still row sequence | `A1 A2 A3 B1 B2 B3` in every output | **FAIL**; expected `A1 B1 A2 B2 A3 B3` |
| Table, column-emitted | `A1 A2 A3 B1 B2 B3`; 6 blocks/6 lines | 6 blocks, column sequence | `A1 A2 A3 B1 B2 B3` in every output | **FAIL**, same expected row pairs |
| Both prose representations | Row-emitted initially interleaves L/R; column-emitted is column-first | 6 blocks | `L1 L2 L3 R1 R2 R3`, with full sentences preserved | **PASS** in all outputs |
| Reversed word objects | `BETA ALPHA`; 1 block/2 lines | Unchanged | `ALPHA BETA` | **PASS**; historical failure no longer persists |
| Other spacing/fragmentation and repeated Forms | Original observations preserved | — | All six cases meet original oracles, including both `STAMP` occurrences | **PASS** in all outputs |

The complete full-string comparisons, expected row-pair adjacency and prose column chains are in `acceptance.json`. All three expected table row adjacencies are absent from each export. HTML DOM order is scored, not spatial display. Markdown scoring removes only exact page/navigation/limitations boundaries and reads the bounded frozen Courier `<code>` content with HTML entity decoding. Unexpected body syntax fails rather than being discarded. This is not a general Markdown parser.

All 11 parser JSON captures and all 22 rendered PNGs match the historical experiment byte-for-byte, despite different platform/Python metadata. Text/Markdown/HTML exports also match history for all cases except reversed word objects. That case changes in each format from `BETA ALPHA` to `ALPHA BETA`. See `baseline-comparison.json`.

Within all four representation groups, full RGB sample hashes match at both 72 and 144 DPI. The table and prose renders were inspected; visual layout is consistent with the synthetic placements. This is visual-equivalence evidence only. Correct pixels and representation-invariant extraction do not establish correct semantic relationships.

## Causal boundary and defensible scope

`analysis._split_column_blocks` splits whitespace-separated row blocks; `_reading_order` then prioritizes a vertical gutter. For the row-emitted table, the parser initially retains the author-defined pairing, and application ordering destroys it. For the column-emitted table, the parser already returns columns. The same application rule makes both prose representations meet their column-first oracle.

HTML and text/Markdown share `_ordered_blocks`; changing only an exporter would create inconsistent results. Merely preserving parser order would leave the column-emitted table wrong and break the row-emitted prose. Globally choosing row-first order would break the positive prose controls. Token shape, token length or fixture names are not evidence of table semantics. These untagged PDFs do not provide the application with the external author intent recorded in the oracle.

No production heuristic was developed or tuned. There is no claim of universal impossibility, general reading-order correctness, representative accuracy, or held-out semantic performance. Any future automatic correction must first freeze independent competing controls—including short column prose and long paired records, multiple emission orders, ambiguous identical-layout interpretations, repeated placements, bidi/rotated text and equations—then pass table relationships and all prose/occurrence/export controls without fixture-specific rules. These existing cases remain regression evidence, not held-out evidence for that change.

## Executable acceptance gates

The limitation gate passes only when original inputs and protocol hashes remain intact, two complete captures are byte-identical, all nine positive cases pass in all three exports, all six scorer controls behave as declared, the pinned main is identified, and both table representations reproduce the **exact historical column-first sequence** in all three formats. Empty, truncated, reversed or otherwise wrong table output is not accepted as evidence that the same failure persists.

The six scorer controls include two accepted correct sequences and four rejected mutations: column-first paired records, row-first prose, a dropped repeated occurrence, and reversed words. They test scorer sensitivity; they are not unseen PDFs or validation of an automatic semantic detector.

- `--mode limitation`: **exit 0**, recommendation evidence accepted.
- `--mode correction`: **exit 1**, all-oracle semantic acceptance rejected because both table cases fail in all three exports.
- Capture completion has its own success status and never implies semantic acceptance.
- Each final run contains 78 files; all 78 match the second fresh run byte-for-byte. `output-sha256.json` records both complete inventories.

## Reproduce

Use a checkout of the pinned main with this experiment overlay (or this PR branch). Keep existing evidence; use new output paths:

```sh
uv sync --locked --extra html
export PYTHONPATH="$PWD/src"
uv run --locked python experiments/representation_v1/build_fixtures.py --check
uv run --locked --extra html python experiments/table_prose_current_v1/capture.py --output /tmp/table-prose-a
uv run --locked --extra html python experiments/table_prose_current_v1/capture.py --output /tmp/table-prose-b
uv run --locked python experiments/table_prose_current_v1/accept.py /tmp/table-prose-a --repeat /tmp/table-prose-b --mode limitation
uv run --locked python experiments/table_prose_current_v1/accept.py /tmp/table-prose-a --repeat /tmp/table-prose-b --mode correction
```

The last command must return 1 for this unchanged implementation. Existing output paths are refused. Set `UV_CACHE_DIR` to a writable local path if needed. Explicit `PYTHONPATH` avoids the local editable-install discovery issue; the adapter enforces the imported path and git-object bytes. Two captures in one environment should match exactly; cross-machine summary paths/platform metadata can differ and renderer changes require inspection. Never update the original oracle to make reproduction pass.

## Validation and handoff

- Relevant analysis/LTR/PDF-boundary suite: **159 tests and 21 subtests passed**.
- Ruff format/check, TypeScript check/test, Spectral and ShellCheck passed. Locked dependency audit: no known vulnerabilities or adverse statuses in 149 packages.
- Full local suite was run: reported **164 failed, 292 passed, 2 skipped** (including subtest outcomes). A directly examined CLI worker failure is `ValueError: current limit exceeds maximum limit`; worker resource-limit setup is an environment gate. The experiment exercises analysis/exports directly, and production source is unchanged. Do not describe the full local suite as passing or classify every failure without further inspection.
- Hosted CI for this experiment PR is recorded separately after submission; prior PR #11 CI is not evidence for this branch.
- Architecture, security, data-integrity and planning consultations were consolidated before implementation. Correctness review found and resolved import-provenance and overly broad negative-gate checks.
- Next: review the bounded limitation and retain these fixtures as regression controls; any proposed automatic correction needs independently frozen semantic acceptance evidence first. No merge or release is requested.
