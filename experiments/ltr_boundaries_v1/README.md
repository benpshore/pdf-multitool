# Independent real-PDF boundary qualification

Base: 1bfbe98740e75b534ce6c9ef87a88aefbf39742d (merged PR10).
Freeze contracts, deterministic builder, original PDFs and hashes before
extracting or visually inspecting them. Never replace frozen inputs to fit
observations. PR8 and PR10 acceptance artifacts remain unchanged.

All PDFs are synthetic, one page, 300 x 180 points, two text objects emitted
right fragment first. No user files, external resources or new dependencies.
Mixed-direction fixtures embed DejaVu Sans for actual Hebrew glyph coverage;
they deliberately do not claim correct linguistic shaping or bidi semantics.

Observe raw dict, rawdict, texttrace, local repair, final ordered blocks and
text/Markdown/HTML exports. Render complete pages at 72 and 216 DPI, visually
inspect both scales, and report model visual observations as geometry checks,
not independent semantic adjudication. SHA-256 checks preserve input bytes.

The contract is explicit: retain local parser order in exclusion cases;
apply spatial order to the positive and closely adjacent alphabetic labels.
The latter exposes an acknowledged ambiguity, not a hidden prose/table oracle.
Global gutter ordering is reported separately from the local repair.

For malformed coordinates, safe unit dictionaries inject nonfinite values in
bbox/origin components; do not feed malformed numeric tokens to native engines.
The desired local behavior is conservative retention of the original block.
