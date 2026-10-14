# Explicit form operations

These operations use the existing pinned PyMuPDF/MuPDF runtime. They do not infer
personal answers, fill signature fields, sign documents or validate cryptographic
signatures. Editing a signed PDF generally invalidates its signatures.

## Inspect and fill

The GUI's form action menu selects **Inspect fields**, **Fill existing fields** or
**Create fields**. Inspection reports names, types, rectangles, choices, maximum
lengths and button export values. Filling uses a JSON object with exact field names:

```json
{"Name":"Example","Agreed":true,"Delivery":"Mail"}
```

Checkboxes take true/false. Radio groups take exactly one existing export value;
booleans, unknown values, duplicate export values and ambiguous independent groups
with the same name are rejected. Shared-parent radio widgets can span pages. Both
the parent value and each widget's appearance state are saved and verified.

Read-only and signature fields cannot be filled. Supported text values are bounded
Windows-1252/WinAnsi text; unsupported Unicode is rejected rather than silently
replacing scientific symbols. Password/file-select/rich-text fields, multi-select
choices, export/display-paired choices and shared non-radio names are unsupported.
Ordinary multilingual text extraction is a separate capability and is not limited
to the form editor's base-font character set.

## Create fields

```json
[
  {"page":1,"name":"Name","type":"text","rect":[40,60,260,90],"max_length":50,"label":"Full name"},
  {"page":1,"name":"Agreed","type":"checkbox","rect":[40,110,60,130],"value":false},
  {"page":1,"name":"Delivery","type":"combobox","rect":[40,150,260,180],"choices":["Mail","Pickup"],"value":"Mail"}
]
```

CLI: `python -m pdf_multitool forms document.pdf --create-fields-json fields.json`.
Filling uses `--fields-json values.json`. Add `--flatten` to either mode to bake
widget appearances into page content. The two JSON modes are mutually exclusive;
flattening without an explicit fill/create operation is rejected.

Creation supports text, checkbox, combobox and listbox, up to 200 definitions per
operation. Pages are one-based. Rectangles are finite **unrotated PDF coordinates**
and must be inside the page. New rectangles cannot overlap existing/new widgets.
Names must be unique against the full field hierarchy, including nonvisual fields.
Names containing unsupported hierarchy/control characters and unknown keys fail.

Optional keys: `value`, `label`, `required`, `read_only`, `font_size` (6–36 points
for non-checkbox fields), `max_length` (text only), and `choices` (choice types).
New visible text is single-line Windows-1252 in Helvetica. Labels become AcroForm
tooltips; this is not a PDF/UA accessibility certification. New radio/signature
fields, embedded scripts, resource URLs, custom fonts and inferred values are not
supported. Values and choices must fit the requested initial field dimensions.

## Validation, privacy and boundaries

All requested edits are validated before mutation. A late engine failure causes
the disposable working document and partial files to be discarded. Saved outputs
are reopened to check page counts, field metadata, values, radio states and normal
appearances. Synthetic tests compare rendered appearances, including flattening.

CLI JSON is read from a regular local file with a 128 KiB cap; API job bodies have
a 32 KiB cap. Duplicate JSON keys, nonfinite constants and excessive nesting fail.
No configuration URL is fetched. Filled/created values necessarily appear in the
requested PDF, but internal verification snapshots are omitted from change reports.
Terminal jobs drop form-value references, and accepted GUI submissions clear the
entry box. Python does not guarantee memory zeroization.

One supplied document password is used for the selected batch. Different per-file
passwords are not supported. Merging or editing protected documents creates an
unencrypted working copy unless Protect PDF is used; the original remains intact.
The byte-preserving Rename operation retains original protection.

These are development capabilities with synthetic regression evidence. Native
parser provenance, hostile-document isolation and real-browser/mobile validation
retain the limitations recorded elsewhere in this project.

## Copying forms through merge and split (0.4.2)

Complete ascending consecutive ranges preserve multi-page radio groups with
reopened fillability checks. Partial cross-page radio groups and reordered or
nonconsecutive selections involving shared field names are rejected before an
output is published. Choose a complete consecutive range or explicitly flatten
a separate copy if editable fields are unnecessary. Flattening is never inferred.

The installed engine can rename duplicate field names when copying complete
ranges or merging repeated inputs. Jobs disclose the number of renamed widgets;
review output field names before using a fill mapping. Repeated text/checkbox
values and independently fillable duplicate radio inputs are covered by tests.
A page-wise widget-count check also rejects unexpected engine omissions. This is
not a guarantee for every AcroForm hierarchy or XFA document.
