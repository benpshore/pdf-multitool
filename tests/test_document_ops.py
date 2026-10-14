"""Real synthetic document tests using preinstalled PyMuPDF, not a verified release.

All fixtures are generated locally. The dummy signature dictionary tests presence
reporting only and is deliberately not a cryptographically valid signature.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import pymupdf

from pdf_multitool.core import Job, JobError


class DocumentOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plain = self.root / "synthetic.pdf"
        with pymupdf.open() as doc:
            for number in range(2):
                page = doc.new_page(width=400, height=400)
                page.insert_text((36, 50), f"Preserved native text, page {number + 1}")
            doc.set_metadata({"title": "Synthetic document"})
            doc.save(self.plain)

    def job(self, operation, path=None, options=None):
        path = path or self.plain
        before = hashlib.sha256(path.read_bytes()).digest()
        job = Job(operation, [{"id": "synthetic", "name": path.name, "path": str(path)}],
                  options if options is not None else {}, self.root / "jobs")
        job.run()
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before,
                         "An operation modified its input")
        return job

    def output(self, job, suffix=".pdf", filename=None):
        self.assertEqual(job.state, "completed", job.errors)
        paths = [Path(item["path"]) for item in job.results
                 if (Path(item["path"]).name == filename if filename else
                     Path(item["path"]).suffix == suffix)]
        self.assertEqual(len(paths), 1, job.results)
        self.assertGreater(paths[0].stat().st_size, 0)
        return paths[0]

    def assert_failed_cleanly(self, job, fragment=None):
        self.assertEqual(job.state, "failed", job.snapshot())
        self.assertEqual(job.results, [])
        self.assertTrue(job.errors)
        if fragment:
            self.assertIn(fragment.lower(), job.errors[0]["error"].lower())
        self.assertEqual(list(job.directory.rglob("*")), [],
                         "Failed job left partial outputs or diagnostics behind")

    def assert_no_password_on_disk(self, job, *passwords):
        snapshot = json.dumps(job.snapshot()).encode()
        for password in passwords:
            encoded = password.encode()
            self.assertNotIn(encoded, snapshot)
            for path in job.directory.rglob("*"):
                if path.is_file():
                    self.assertNotIn(encoded, path.read_bytes(), str(path))
        self.assertNotIn("password", job.options)
        self.assertNotIn("new_password", job.options)

    def encrypted_fixture(self, password="Input-secret-978203"):
        path = self.root / "encrypted.pdf"
        with pymupdf.open(self.plain) as doc:
            doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                     user_pw=password, owner_pw=password)
        return path, password

    def forms_fixture(self):
        path = self.root / "forms.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page(width=400, height=400)
            specs = [
                ("full_name", pymupdf.PDF_WIDGET_TYPE_TEXT, (40, 40, 300, 80)),
                ("consent", pymupdf.PDF_WIDGET_TYPE_CHECKBOX, (40, 100, 60, 120)),
                ("choice", pymupdf.PDF_WIDGET_TYPE_COMBOBOX, (40, 140, 180, 170)),
                ("locked", pymupdf.PDF_WIDGET_TYPE_TEXT, (40, 180, 180, 210)),
                ("unsigned", pymupdf.PDF_WIDGET_TYPE_SIGNATURE, (40, 240, 180, 280)),
                ("dummy_present", pymupdf.PDF_WIDGET_TYPE_SIGNATURE, (40, 300, 180, 340)),
            ]
            for name, kind, rect in specs:
                widget = pymupdf.Widget()
                widget.field_name, widget.field_type = name, kind
                widget.rect = pymupdf.Rect(rect)
                widget.text_font, widget.text_fontsize = "Helv", 16
                widget.text_color = (0, 0, 0)
                if name == "choice":
                    widget.choice_values = ["Alpha", "Beta"]
                if name == "locked":
                    widget.field_flags = 1
                    widget.field_value = "Read only"
                page.add_widget(widget)
            for widget in page.widgets():
                if widget.field_name == "dummy_present":
                    sig = doc.get_new_xref()
                    doc.update_object(sig, "<< /Type /Sig /Filter /Adobe.PPKLite "
                                      "/SubFilter /adbe.pkcs7.detached "
                                      "/ByteRange [0 0 0 0] /Contents <00> >>")
                    doc.xref_set_key(widget.xref, "V", f"{sig} 0 R")
            doc.save(path)
        return path

    @staticmethod
    def dark_pixels(page, clip):
        pix = page.get_pixmap(clip=pymupdf.Rect(clip), alpha=False)
        pixels = pix.samples
        return sum(any(component < 180 for component in pixels[i:i + pix.n])
                   for i in range(0, len(pixels), pix.n))

    def test_password_missing_wrong_and_correct(self):
        path, password = self.encrypted_fixture()
        for supplied in (None, "Wrong-secret-39784"):
            with self.subTest(password="missing" if supplied is None else "wrong"):
                job = self.job("unlock", path, {} if supplied is None else {"password": supplied})
                self.assert_failed_cleanly(job, "missing or incorrect")
                self.assert_no_password_on_disk(job, password, supplied or "unused-password")
        job = self.job("unlock", path, {"password": password})
        with pymupdf.open(self.output(job)) as doc:
            self.assertFalse(doc.needs_pass)
            self.assertEqual(len(doc), 2)
            self.assertIn("Preserved native text, page 1", doc[0].get_text())
        self.assert_no_password_on_disk(job, password)

    def test_identify_protected_document_without_password(self):
        path, _ = self.encrypted_fixture()
        job = self.job("identify", path)
        report = json.loads(self.output(job, suffix=".json").read_text())
        self.assertTrue(report["input_protected"])
        self.assertEqual(report["authentication"], "password_required")

    def test_rotated_redaction_uses_unrotated_coordinates(self):
        path = self.root / "rotated.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page(width=200, height=100)
            page.insert_text((130, 50), "SECRET", fontsize=10)
            box = list(page.search_for("SECRET")[0])
            page.set_rotation(90)
            doc.save(path)
        job = self.job("redact", path, {"redact_boxes": [{"page": 1, "rect": box}]})
        with pymupdf.open(self.output(job)) as doc:
            self.assertNotIn("SECRET", doc[0].get_text())
            self.assertEqual(doc[0].rotation, 90)
        bad = self.job("redact", path, {"redact_boxes": [{"page": 1, "rect": [5, 150, 30, 190]}]})
        self.assert_failed_cleanly(bad, "inside")

    def test_redaction_removes_accessibility_alternative_text(self):
        secret = "TAGGED_SECRET_857493"
        path = self.root / "tagged.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((40, 80), secret)
            tree, element = doc.get_new_xref(), doc.get_new_xref()
            doc.update_object(tree, f"<< /Type /StructTreeRoot /K [{element} 0 R] >>")
            doc.update_object(element, f"<< /Type /StructElem /S /P /P {tree} 0 R /Pg {page.xref} 0 R /Alt ({secret}) /ActualText ({secret}) /K 0 >>")
            doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{tree} 0 R")
            doc.xref_set_key(doc.pdf_catalog(), "MarkInfo", "<< /Marked true >>")
            doc.xref_set_key(page.xref, "StructParents", "0")
            doc.save(path)
        job = self.job("redact", path, {"redact_text": secret})
        output = self.output(job)
        self.assertNotIn(secret.encode(), output.read_bytes())
        with pymupdf.open(output) as doc:
            self.assertEqual(doc.xref_get_key(doc.pdf_catalog(), "StructTreeRoot")[0], "null")
            for xref in range(1, doc.xref_length()):
                self.assertNotIn(secret, doc.xref_object(xref))
                if doc.xref_is_stream(xref):
                    self.assertNotIn(secret.encode(), doc.xref_stream(xref))

    def test_redaction_removes_pieceinfo_and_ignores_pending_annotations(self):
        path = self.root / "pieceinfo.pdf"
        secret = "CUSTOM_SECRET_629382"
        with pymupdf.open() as doc:
            first = doc.new_page()
            first.insert_text((40, 80), secret)
            doc.xref_set_key(first.xref, "PieceInfo", f"<< /Private << /Text ({secret}) >> >>")
            second = doc.new_page()
            second.insert_text((40, 80), "KEEP THIS")
            second.add_redact_annot(second.search_for("KEEP THIS")[0])
            doc.xref_set_key(doc.pdf_catalog(), "PieceInfo", f"<< /Private << /Text ({secret}) >> >>")
            doc.save(path)
        job = self.job("redact", path, {"redact_text": secret, "pages": "1"})
        output = self.output(job)
        with pymupdf.open(output) as doc:
            self.assertNotIn(secret, doc[0].get_text())
            self.assertIn("KEEP THIS", doc[1].get_text())
            self.assertEqual(doc.xref_get_key(doc.pdf_catalog(), "PieceInfo")[0], "null")
        self.assertNotIn(secret.encode(), output.read_bytes())

    def test_unknown_residual_target_fails_closed(self):
        path = self.root / "custom-reservoir.pdf"
        secret = "RESIDUAL_SECRET_849378"
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((40, 80), secret)
            doc.xref_set_key(doc.pdf_catalog(), "CustomPrivate", f"({secret})")
            doc.save(path)
        self.assert_failed_cleanly(self.job("redact", path, {"redact_text": secret}), "residual")

    def test_rename_preserves_bytes_and_uses_safe_output_name(self):
        job = self.job("rename")
        output = self.output(job)
        self.assertEqual(output.read_bytes(), self.plain.read_bytes())
        name = next(r["name"] for r in job.results if r["path"].endswith(".pdf"))
        self.assertNotIn("/", name)
        self.assertTrue(name.endswith(".pdf"))

    def test_encrypted_rename_warning_matches_preserved_protection(self):
        path, password = self.encrypted_fixture()
        job = self.job("rename", path, {"password": password})
        output = self.output(job)
        self.assertEqual(output.read_bytes(), path.read_bytes())
        self.assertTrue(any("preserves its original encryption" in w for w in job.warnings))
        self.assertFalse(any("outputs are unencrypted" in w.lower() for w in job.warnings))

    def test_signature_value_inherited_from_parent_field(self):
        source = self.forms_fixture()
        target = self.root / "parent-signature.pdf"
        with pymupdf.open(source) as doc:
            page = doc[0]
            widget = next(w for w in page.widgets() if w.field_name == "dummy_present")
            kind, value = doc.xref_get_key(widget.xref, "V")
            self.assertEqual(kind, "xref")
            parent = doc.get_new_xref()
            doc.update_object(parent, f"<< /FT /Sig /T (dummy_present) /Kids [{widget.xref} 0 R] /V {value} >>")
            doc.xref_set_key(widget.xref, "V", "null")
            doc.xref_set_key(widget.xref, "T", "null")
            doc.xref_set_key(widget.xref, "Parent", f"{parent} 0 R")
            doc.save(target)
        job = self.job("signatures", target)
        report = json.loads(self.output(job, suffix=".json").read_text())
        signature = next(s for s in report["signature_fields"] if s["name"] == "dummy_present")
        self.assertTrue(signature["value_present"])
        self.assertEqual(signature["cryptographic_validity"], "not_verified")

    def test_forms_reject_radio_ambiguity_and_text_maxlen(self):
        path = self.root / "constrained-fields.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            for name, kind, rect in [("radio", pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, (40, 40, 60, 60)),
                                     ("short", pymupdf.PDF_WIDGET_TYPE_TEXT, (40, 80, 160, 105))]:
                widget = pymupdf.Widget()
                # This PyMuPDF version cannot create radio groups directly; create
                # a checkbox appearance, then set the standard PDF radio flag.
                initial_kind = pymupdf.PDF_WIDGET_TYPE_CHECKBOX if kind == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON else kind
                widget.field_name, widget.field_type, widget.rect = name, initial_kind, pymupdf.Rect(rect)
                if name == "short":
                    widget.text_maxlen = 3
                created = page.add_widget(widget)
                if kind == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
                    doc.xref_set_key(created.xref, "Ff", "32768")
            doc.save(path)
        self.assert_failed_cleanly(self.job("forms", path, {"fields": {"radio": True}}), "Radio group")
        self.assert_failed_cleanly(self.job("forms", path, {"fields": {"short": "123456789"}}), "maximum length")

    def test_protect_aes256_then_unlock_roundtrip(self):
        password = "New-secret-64820791"
        protect = self.job("protect", options={"new_password": password})
        protected = self.output(protect)
        with pymupdf.open(protected) as doc:
            self.assertTrue(doc.needs_pass)
            self.assertEqual(doc.authenticate("wrong"), 0)
            self.assertNotEqual(doc.authenticate(password), 0)
            self.assertIn("256-bit AES", doc.metadata["encryption"])
            self.assertEqual(len(doc), 2)
        unlock = self.job("unlock", protected, {"password": password})
        with pymupdf.open(self.output(unlock)) as doc, pymupdf.open(self.plain) as original:
            self.assertFalse(doc.needs_pass)
            self.assertEqual([p.get_text() for p in doc], [p.get_text() for p in original])
        self.assert_no_password_on_disk(protect, password)
        self.assert_no_password_on_disk(unlock, password)

    def test_protect_rejects_invalid_password_boundaries(self):
        for value in (None, "short", "a" * 128, "é" * 64, 123):
            with self.subTest(value_type=type(value).__name__, length=len(str(value))):
                self.assert_failed_cleanly(self.job("protect", options={"new_password": value}), "8-127")

    def test_input_password_type_and_length_rejected(self):
        for value in (123, ["private-value"], "a" * 128, "é" * 64):
            with self.subTest(value_type=type(value).__name__):
                self.assert_failed_cleanly(self.job("clean", options={"password": value}), "Invalid document password")

    def test_forms_enumeration_keeps_signatures_separate(self):
        job = self.job("forms", self.forms_fixture())
        report = json.loads(self.output(job, filename="forms.json").read_text())
        fields = {field["name"]: field for field in report["fields"]}
        self.assertEqual(set(fields), {"full_name", "consent", "choice", "locked"})
        self.assertEqual(fields["full_name"]["type"], "Text")
        self.assertTrue(fields["locked"]["readonly"])
        self.assertEqual({s["name"] for s in report["signature_fields"]}, {"unsigned", "dummy_present"})

    def test_forms_fill_reopen_values_and_visible_appearance(self):
        path = self.forms_fixture()
        with pymupdf.open(path) as doc:
            blank_pixels = self.dark_pixels(doc[0], (45, 43, 295, 77))
        values = {"full_name": "Synthetic Filled Name", "consent": True, "choice": "Beta"}
        job = self.job("forms", path, {"fields": values})
        with pymupdf.open(self.output(job)) as doc:
            page = doc[0]
            widgets = {w.field_name: w for w in page.widgets()}
            self.assertEqual(widgets["full_name"].field_value, values["full_name"])
            self.assertEqual(widgets["consent"].field_value, widgets["consent"].on_state())
            self.assertEqual(widgets["choice"].field_value, "Beta")
            self.assertGreater(self.dark_pixels(doc[0], (45, 43, 295, 77)), blank_pixels + 100)

    def test_forms_checkbox_false_clears_a_previously_checked_box(self):
        checked = self.job("forms", self.forms_fixture(), {"fields": {"consent": True}})
        cleared = self.job("forms", self.output(checked), {"fields": {"consent": False}})
        with pymupdf.open(self.output(cleared)) as doc:
            page = doc[0]
            widget = next(w for w in page.widgets() if w.field_name == "consent")
            self.assertEqual(widget.field_value, "Off")

    def test_forms_checkbox_false_and_flatten_removes_widgets(self):
        values = {"full_name": "Flattened Synthetic Name", "consent": False}
        job = self.job("forms", self.forms_fixture(), {"fields": values, "flatten": True})
        with pymupdf.open(self.output(job)) as doc:
            self.assertEqual(list(doc[0].widgets() or []), [])
            self.assertIn(values["full_name"], doc[0].get_text())
            self.assertGreater(self.dark_pixels(doc[0], (45, 43, 295, 77)), 100)
        detail = json.loads(self.output(job, filename="changes.json").read_text())
        self.assertTrue(detail["flattened"])

    def test_forms_reject_invalid_fields_without_partial_output(self):
        path = self.forms_fixture()
        cases = [({}, "nonempty"), ([], "nonempty"),
                 ({"unknown": "value"}, "Unknown"),
                 ({"full_name": "would be filled", "unknown": "value"}, "Unknown"),
                 ({"locked": "change"}, "read-only"),
                 ({"unsigned": "not a signature"}, "Signature"),
                 ({"consent": "yes"}, "true or false"),
                 ({"choice": "Not in choices"}, "allowed choices"),
                 ({"full_name": 13}, "bounded string")]
        for fields, message in cases:
            with self.subTest(fields=fields):
                self.assert_failed_cleanly(self.job("forms", path, {"fields": fields}), message)

    def test_signatures_report_presence_never_cryptographic_validation(self):
        job = self.job("signatures", self.forms_fixture())
        report = json.loads(self.output(job, filename="signatures.json").read_text())
        signatures = {s["name"]: s for s in report["signature_fields"]}
        self.assertFalse(signatures["unsigned"]["value_present"])
        self.assertTrue(signatures["dummy_present"]["value_present"])
        self.assertTrue(all(s["cryptographic_validity"] == "not_verified" for s in signatures.values()))
        self.assertIn("not cryptographic verification", report["signature_warning"])
        self.assertTrue(any("verification and digital signing are not implemented" in w for w in job.warnings))

    def redaction_fixture(self):
        path = self.root / "redaction.pdf"
        secret = "REMOVE_SECRET_628491"
        with pymupdf.open() as doc:
            page = doc.new_page(width=400, height=400)
            page.insert_text((36, 50), secret)
            page.insert_text((36, 90), "Keep this public sentence")
            # Two solid halves make underlying image-pixel removal measurable.
            samples = bytes((255, 0, 0) * 40 + (0, 255, 0) * 40) * 40
            pix = pymupdf.Pixmap(pymupdf.csRGB, 80, 40, samples, False)
            page.insert_image(pymupdf.Rect(180, 180, 340, 260), pixmap=pix)
            doc.set_metadata({"title": secret, "subject": secret})
            doc.set_xml_metadata(f"<synthetic>{secret}</synthetic>")
            doc.embfile_add("secret.txt", secret.encode(), filename="secret.txt")
            page.add_file_annot((340, 300), secret.encode(), "annotation-secret.txt")
            page.add_text_annot((300, 330), secret)
            page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(40, 110, 160, 125),
                              "uri": "https://example.invalid/" + secret})
            widget = pymupdf.Widget()
            widget.field_name, widget.field_type = "secret_field", pymupdf.PDF_WIDGET_TYPE_TEXT
            widget.rect, widget.field_value = pymupdf.Rect(40, 130, 160, 160), secret
            page.add_widget(widget)
            doc.set_toc([[1, secret, 1]])
            doc.save(path)
        return path, secret

    def test_redaction_removes_text_reservoirs_and_underlying_image_pixels(self):
        path, secret = self.redaction_fixture()
        job = self.job("redact", path, {"redact_text": secret,
                        "redact_boxes": [{"page": 1, "rect": [180, 180, 260, 260]}]})
        output = self.output(job)
        with pymupdf.open(output) as doc:
            page = doc[0]
            self.assertNotIn(secret, page.get_text())
            self.assertEqual(page.search_for(secret), [])
            self.assertIn("Keep this public sentence", page.get_text())
            self.assertNotIn(secret, str(doc.metadata))
            self.assertEqual(doc.get_xml_metadata(), "")
            self.assertEqual(doc.embfile_count(), 0)
            self.assertEqual(doc.get_toc(), [])
            self.assertEqual(page.get_links(), [])
            self.assertEqual(list(page.annots() or []), [])
            self.assertEqual(list(page.widgets() or []), [])
            rendered = page.get_pixmap(alpha=False)
            self.assertEqual(rendered.pixel(220, 220), (0, 0, 0))
            self.assertEqual(rendered.pixel(300, 220), (0, 255, 0))
            images = page.get_images(full=True)
            self.assertEqual(len(images), 1)
            image = pymupdf.Pixmap(doc, images[0][0])
            self.assertEqual(image.pixel(20, 20), (255, 255, 255),
                             "Underlying image retained pixels under the black overlay")
            self.assertEqual(image.pixel(60, 20), (0, 255, 0))
            for xref in range(1, doc.xref_length()):
                self.assertNotIn(secret, doc.xref_object(xref), f"Object {xref}")
                if doc.xref_is_stream(xref):
                    stream = doc.xref_stream(xref)
                    self.assertNotIn(secret.encode(), stream, f"Stream {xref}")
                    self.assertNotIn(secret.encode().hex().encode(), stream, f"Hex stream {xref}")
        self.assertNotIn(secret.encode(), output.read_bytes())
        self.assertTrue(any("text matching alone cannot find" in warning for warning in job.warnings))

    def test_redaction_selected_pages_preserves_unselected_native_text(self):
        job = self.job("redact", options={"redact_text": "Preserved native text", "pages": "1"})
        with pymupdf.open(self.output(job)) as doc:
            self.assertNotIn("Preserved native text", doc[0].get_text())
            self.assertIn("Preserved native text", doc[1].get_text())

    def test_redaction_rejects_nonfinite_coordinates_and_unselected_boxes(self):
        for rect in ([0, 0, float("nan"), 50], [0, 0, float("inf"), 50],
                     [0, 0, True, 50], [0, 0, "50", 50], [0, 0, 50]):
            with self.subTest(rect=rect):
                self.assert_failed_cleanly(self.job("redact", options={
                    "redact_boxes": [{"page": 1, "rect": rect}]}))
        self.assert_failed_cleanly(self.job("redact", options={"pages": "1",
            "redact_text": "Preserved native text",
            "redact_boxes": [{"page": 2, "rect": [10, 10, 30, 30]}]}))

    def test_redaction_no_match_fails_without_output(self):
        self.assert_failed_cleanly(self.job("redact", options={"redact_text": "NEVER PRESENT 734842"}),
                                   "No redaction targets")

    def test_redaction_rejects_missing_and_malformed_specs(self):
        for options in ({}, {"redact_text": 123}, {"redact_boxes": "bad"},
                        {"redact_boxes": [{"page": 1}]},
                        {"redact_boxes": [{"page": 1, "rect": [0, 0, 500, 500]}]},
                        {"redact_boxes": [{"page": 1, "rect": [5, 5, 5, 5]}]}):
            with self.subTest(options=options):
                self.assert_failed_cleanly(self.job("redact", options=options))

    def test_redaction_invalid_box_page_cannot_silently_succeed(self):
        for page in (0, 99, True):
            with self.subTest(page=page):
                job = self.job("redact", options={"redact_text": "Preserved native text",
                               "redact_boxes": [{"page": page, "rect": [180, 180, 260, 260]}]})
                self.assert_failed_cleanly(job)

    def test_clean_preserves_native_text_and_page_rendering(self):
        job = self.job("clean")
        with pymupdf.open(self.output(job)) as doc, pymupdf.open(self.plain) as source:
            self.assertEqual(len(doc), len(source))
            for actual, expected in zip(doc, source):
                self.assertEqual(actual.get_text(), expected.get_text())
                self.assertEqual(actual.get_pixmap(alpha=False).samples,
                                 expected.get_pixmap(alpha=False).samples)
        changes = json.loads(self.output(job, filename="changes.json").read_text())
        self.assertIn("deskew", changes["not_performed"])

    def test_identify_and_pdfhtml_create_reopenable_reports(self):
        for operation in ("identify", "pdfhtml"):
            with self.subTest(operation=operation):
                job = self.job(operation)
                report = json.loads(self.output(job, filename="analysis.json").read_text())
                self.assertFalse(report["input_protected"])
                self.assertIn("forms_and_signatures", report)
                if operation == "pdfhtml":
                    html = self.output(job, suffix=".html").read_text()
                    self.assertIn("Preserved native text", html)
                    self.assertIn("<html", html.lower())

    def test_malformed_inputs_fail_atomically_for_document_operations(self):
        path = self.root / "malformed.pdf"
        path.write_bytes(b"%PDF-1.7\nnot a real PDF")
        for operation, options in (("identify", {}), ("pdfhtml", {}), ("clean", {}),
                                   ("forms", {}), ("signatures", {}), ("redact", {"redact_text": "x"}),
                                   ("unlock", {}), ("protect", {"new_password": "Valid-new-password"})):
            with self.subTest(operation=operation):
                self.assert_failed_cleanly(self.job(operation, path, options))

    def test_unknown_operation_rejected_before_directory_creation(self):
        with self.assertRaisesRegex(JobError, "Unknown operation"):
            self.job("unsupported-operation")
        self.assertFalse((self.root / "jobs").exists())


if __name__ == "__main__":
    unittest.main()
