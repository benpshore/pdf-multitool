# Import validation

Checked 2026-10-14 against the integrated `src/` tree, with preinstalled Python
3.12 and native engines (not the destination's Python 3.14 environment):

- 247 unittest regressions passed.
- 13 fail-closed artifact-verifier tests passed.
- Six mocked DOM/API scenarios passed.
- Python compilation and JavaScript syntax checks passed.
- Existing TypeScript runtime smoke test passed (one test).

The imported package's CLI parser smoke assertion also passed. The old template
smoke test was adapted from its placeholder greeting to the real CLI parser.

Required checks not established locally:

- Ruff, pytest, native package build and Python 3.14 compatibility: the required
  interpreter/development tools are absent. No runtime was downloaded.
- Dependency audit: requires network and a verified dependency environment.
- OpenAPI lint: Spectral was unavailable and registry access was blocked.
- Shellcheck and TypeScript typecheck: executables were unavailable.
- `uv.lock` needs an authorized update for the newly declared engine dependencies.

These limitations are blockers for merge/readiness, not passing checks. The
destination's required CI and release protections have not been relaxed.
