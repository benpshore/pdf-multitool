# Frozen structured-cell counterexamples

The generator, authored grids and 27 PDFs were frozen before measuring parsers or
the product. `FROZEN.json` binds their bytes; `PRECAPTURE.json` records the local
time and starting commit. This is local synthetic evidence, not an independent
or representative benchmark. Earlier experiments and oracles are unchanged.

Four families have ruled and borderless versions in row, column and reverse
operator order: numeric strings, missing cells, wrapped cells and a fully empty
row. A fifth family is wrapped numeric prose in two columns, with the same three
operator orders. Numeric strings retain zeros, signs, commas and percentages.
The authored wrapped cells contain literal line breaks. Empty cells and entire
empty rows are part of the grid oracle, not optional whitespace.

Ruled variants also contain text before and after the grid. Expected automatic
disposition is structural projection for ruled grids and ambiguity for all
borderless candidates. This does not alter the authored cell or prose oracles.
An ambiguous result still fails table qualification when the correct grid is
not emitted. In particular, wrapped and empty-row borderless cases remain
known failures rather than successful detections.

Verify frozen bytes with:

```sh
uv run python -m experiments.structured_cells_v1.build_fixtures
uv run pytest tests/test_table_structure.py tests/test_table_structure_workflows.py
```

See `docs/STRUCTURED_TABLE_QUALIFICATION_20261015.md` for native API decisions,
source-occurrence checks, rendering formats and the remaining acceptance gates.
