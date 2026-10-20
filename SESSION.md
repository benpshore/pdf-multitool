Independent table/prose experiment on pinned main fb146a9. Production unchanged.
Original PR8 tree retained byte-for-byte. Final capture run1/run2 each 78 identical files.
Limitation gate passes; correction gate fails as expected. Nine positive fixtures pass all formats;
both table fixtures retain original exact column-first failure. Reversed words now pass.
See experiments/table_prose_current_v1/README.md for evidence, reproduction, limitations and checks.
Full local suite failed with examined worker resource-limit errors; focused suite passes.
Next: open draft research PR, require hosted CI, attach result. Do not merge or claim general correctness.
