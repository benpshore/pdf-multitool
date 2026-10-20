# Explicit reading-order contracts

These fifteen deterministic synthetic PDFs and their author-defined expectations
were frozen before inspecting product results. The generator imports only the
standard library and the unchanged historical PDF byte writer. `FROZEN.json`
binds the generator, exact contracts and PDF bytes; `PRECAPTURE.json` records the
local premeasurement inventory. This is local temporal evidence, not an external
or independently authored held-out benchmark.

Five grids each have row, column and reversed operator-emission variants:

- Numeric cells include leading zeros, signed decimals, thousands separators and percentages.
- Long paired records counter a token-length table/prose classifier.
- Short column prose counters a short-token table classifier.
- Missing cells preserve the order of populated cells. Flat text does not encode
  the empty cell's column identity; no complete table reconstruction is claimed.
- Each ambiguous grid admits both row and column author intentions without a
  single byte changing. The two expected strings intentionally differ. An
  automatic function receiving only those bytes cannot choose both author intents.

The product's `auto` policy remains the historical default. The new `rows` and
`columns` options are explicit geometric policies, not automatic semantic labels.
Both original table fixtures meet their unchanged full-string oracles only when
the caller selects `rows`. Their default-mode failures remain visible in tests
and the visual capture. All original experiments and expectations remain intact.

Run `uv run python -m experiments.row_order_contract_v1.build_fixtures` to verify
the frozen bytes. Run `uv run pytest tests/test_reading_order.py` for cross-format
policy checks, preserved occurrences, unsupported-direction failure/recovery,
invalid requests and real CLI output. The HTTP/browser tests and committed
`scripts/qualify_reading_order.py` exercise actual API/GUI controls and downloads.
