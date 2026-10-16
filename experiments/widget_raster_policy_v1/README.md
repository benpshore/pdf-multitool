# Two-fixture widget raster diagnosis

Ada accepted the bounded diagnosis in [the review record](parent-review.json):
there is no demonstrated native content or geometry loss in these two fixtures;
PDFium's default widget/page raster policy accounts for their pixel difference.
This does not qualify forms or change any renderer defaults, tests, feature
classification, strict oracle, or merge/release/redaction/general-fidelity hold.
See [the diagnosis](../../docs/WIDGET_RASTER_DIAGNOSIS_20261016.md).

## Evidence

`observed/` preserves the reviewed synthetic originals, native outputs, manual
page-content and FreeText derivatives, source-after-render PDFs, lossless PNGs,
decoded appearances, comparison records, and extracted text. These are copied
byte-for-byte from the reviewed archive. The historical JSON and
[seven-page review](historical-review.pdf) retain their pre-review status text;
the subsequent bounded acceptance is recorded separately in `parent-review.json`.

`retained-strict-oracle/` preserves the original failed assertion, fixture,
candidate, render differences, and test-source snapshot. The snapshot has a
`.py.txt` suffix so normal test discovery and formatting cannot alter it; its
bytes are unchanged. The exact assertion is also executable with
`--mode strict-oracle` below. It still fails. A successful diagnosis reproduction
means the same measurements were reproduced, never that the strict oracle passed.

The source checkpoint is `5644fcaf9014928a817dfc0cd470991329e141ec`.
`manifest.json` binds every retained evidence file and the relevant source/test
bytes. Software versions and the PDFium binary hash are in `software.json`.
Source provenance contains public source identifiers only. No private documents,
credentials, raw environment metadata, job directories, or session logs are
included. All 439 earlier tracked experiment files remain unchanged.

## Reproduce

Use nonroot Linux and the repository's native worker prerequisites. From the
checkout root, install its locked dependencies and use new output paths:

```sh
uv sync --locked --extra oxide --extra ocr
PYTHONPATH=src uv run --locked --extra oxide --extra ocr python experiments/widget_raster_policy_v1/reproduce.py --output /tmp/widget-raster-reproduction
PYTHONPATH=src uv run --locked --extra oxide --extra ocr python experiments/widget_raster_policy_v1/reproduce.py --mode strict-oracle
```

The diagnosis command verifies hashes, runs fresh supervised flatten jobs,
independently constructs the manual/FreeText diagnostics, and freshly renders all
90 PDFium and 12 MuPDF comparisons. It compares each PDFium render with the frozen
pixels and uses an independent recursive decoder for complete appearance/resource
graphs. Raw appearance dictionaries are retained in `structure/`; comparison normalizes only the
optional `/FormType` default of 1, which native output writes explicitly. Resource
and font graphs require no such normalization. It also verifies exact fixture
placement, original page content prefixes,
original resources, page geometry, extracted numbers, and the untouched page.
Raw temporary jobs are removed after copying each new native result. Originals
are never rewritten and existing output directories are refused.

The final command deliberately exits **1**: default PDFium parity still fails
with **3,941** and **1,238** changed pixels at 144 DPI. No tolerance, masking,
LCD substitution or expected-failure marker turns that failure into a pass.
Reproduction pins these observations to the recorded renderers; a different
renderer/build may fail the diagnostic comparison and requires review.

The independently reviewed archive SHA-256 was
`234315d94a7837d0ae8916af592aaca9970d5a63a81104516960b849a999afb6`.
This repository subset omits raw environment metadata and redundant historical
source copies. The original archive and all earlier experiment files are preserved.
