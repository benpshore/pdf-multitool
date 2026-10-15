# Frozen acceptance scope

Base: 755bb3755d04939cded8becbd267dfb1a685ad4a. Written before implementation.
PR8's representation_v1 corpus and oracle are separate and must not change.

Repair only a complete text block containing exactly two parser lines, on an
unrotated page. Both lines must have horizontal direction (1, 0), horizontal
writing mode, and exactly one span. Both spans must share font, size, flags,
color and baseline (within 0.01 point); neither may be superscript. Each span
must contain at least two alphabetic characters, all Unicode bidi class L.
Their line and span bounds must agree, be finite, and have equal top/bottom
(within 0.01 point). Only nonoverlapping fragments with a gap of 0.2 to 0.75
font-size units qualify. Order the two lines by x, retaining spans verbatim.
Do not merge lines, globally sort spans, or alter column/gutter rules.

Positive cases: Courier 10pt ALPHA at x40, BETA at x76, baseline60, emitted
forward/reverse; reverse must export ALPHA BETA in text, Markdown and HTML.
Their page pixels must be identical. Also accept lowercase words, three
characters per fragment and boundary gaps (2 and 7.5 points at 10pt).

Negative controls (retain input line sequence in the local repair):
RTL letters; mixed bidi or bidi controls; 90/180/270 degree direction;
rotated page; superscript flag; raised baseline; unequal font/size/color/flags;
overlap; touching; gap below 2 or above 7.5 points; wide paired cells; numeric
or alphanumeric paired cells; isolated letters; punctuation; multiple spans;
three fragments; multiple baselines; nontext block; missing/nonfinite geometry.
Repeated Forms must retain both occurrences, including exact overlays.

These conditions cannot prove prose semantics. Closely adjacent alphabetic
cell labels can be visually indistinguishable from words. This narrow policy
leaves numbers, symbols and wider cells alone, but does not solve general
reading order, table inference or RTL extraction. No claim of those fixes.
