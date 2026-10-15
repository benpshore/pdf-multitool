# Bounded image input

The image extension is a local Python development feature. It uses preinstalled
Pillow 12.3.0 and MuPDF; no new software was downloaded. Codec and deployment
provenance limitations in `provenance.md` still apply.

## Operations

- `imagepdf`: one page per selected JPEG, PNG or JPEG2000 image, in input order.
- `merge`: interleave original PDF pages and normalized image pages in input order.
- `identify`: validated raster dimensions, format, orientation and observed facts.
- `preview`: bounded thumbnail after the same validation and normalization path.
- Archive/folder intake recognizes those image types; it never recursively expands
  nested archives. Archive magic sniffing is not full image decoding. Decoding and
  pixel validation happen in a supervised worker when an image is processed.

No OCR is performed. Image analysis identifies a possible OCR input, not recognized
words or a measured text-content percentage. Unsupported PDF operations give
conversion guidance rather than pretending images already have a native text layer.

## Limits and fidelity

40 million pixels per image; 200 million pixels per conversion; 16,000 pixels per
side; 200 images; 50 MiB/input. Supervised memory, time and output quotas apply too.
All headers and the combined pixel count are checked before batch pixel decoding.
Animated/multiframe inputs and multi-codestream/composited JPX fail explicitly.
Suffix/content mismatches, detected malformed/truncated images and unsupported
codecs fail. Native JPEG decoders can tolerate some damaged entropy streams;
strict framing and decode checks are not proof of an undamaged original.

DPI is explicit, finite and between 36 and 300 (default 144). It determines page
points as oriented-pixels × 72 / DPI, not pixel resampling or embedded-DPI trust.
EXIF orientation is applied. Normalized RGB/RGBA pixel streams are losslessly
encoded; alpha is retained in the PDF and appears over white in the preview.
Normalization can alter high-bit-depth, CMYK or color-profile interpretation;
this is not an archival photographic color-management tool. Original file bytes
remain unchanged and available. New pages do not retain unrelated EXIF/ICC/GPS
metadata. Generated PDF files are exclusively published without overwriting an
existing destination and are reopened to validate page counts/dimensions.

HEIC, Apple RAW, TIFF/GIF, audio and video remain outside this implementation.
The user interface remains visually unverified on real browsers and iPhone Safari.
