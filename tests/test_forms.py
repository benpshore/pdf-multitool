"""Synthetic AcroForm save/reopen and actual raster appearance regressions."""
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

import pymupdf
from reportlab.pdfgen.canvas import Canvas

from pdf_multitool.core import JobError
from pdf_multitool.forms import create_fields, fill_fields, verify_forms


class FormOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def blank(self, rotation=0):
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=500)
        page.insert_text((20, 480), "Original page content")
        page.set_rotation(rotation)
        self.addCleanup(doc.close)
        return doc

    def fixture(self):
        data = io.BytesIO()
        canvas = Canvas(data, pagesize=(400, 500))
        form = canvas.acroForm
        form.radio(name="pick", value="A", x=30, y=420, selected=True)
        form.radio(name="other", value="X", x=100, y=420, selected=True)
        form.radio(name="other", value="Y", x=130, y=420)
        form.textfield(name="plain", x=30, y=350, width=150, height=30, value="Original")
        canvas.drawString(30, 300, "Original page one")
        canvas.showPage()
        form.radio(name="pick", value="B", x=30, y=420)
        canvas.drawString(30, 300, "Original page two")
        canvas.showPage()
        canvas.save()
        doc = pymupdf.open(stream=data.getvalue(), filetype="pdf")
        self.addCleanup(doc.close)
        return doc

    def specs(self):
        return [
            {"page": 1, "name": "full_name", "type": "text", "rect": [20, 20, 260, 55],
             "value": "Ada Example", "max_length": 30, "label": "Full name", "required": True},
            {"page": 1, "name": "agree", "type": "checkbox", "rect": [20, 80, 42, 102], "value": True},
            {"page": 1, "name": "category", "type": "combobox", "rect": [20, 130, 200, 165],
             "choices": ["Alpha", "Beta"], "value": "Beta"},
            {"page": 1, "name": "region", "type": "listbox", "rect": [20, 200, 200, 270],
             "choices": ["East", "West"], "value": "West", "read_only": True},
        ]

    def saved(self, doc, result, *, flattened=False, name="result.pdf"):
        path = self.root / name
        if flattened:
            doc.bake(annots=False, widgets=True)
        doc.save(path, garbage=4, deflate=True)
        verify_forms(path, result["forms_expected"], flattened=flattened)
        reopened = pymupdf.open(path)
        self.addCleanup(reopened.close)
        return reopened

    def objects(self, doc):
        return tuple((doc.xref_object(x), doc.xref_stream(x) if doc.xref_is_stream(x) else None)
                     for x in range(1, doc.xref_length()))

    def reject_atomic(self, doc, function, arguments, fragment=None):
        before = self.objects(doc)
        files_before = {p: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with self.assertRaises(JobError) as caught:
            function(doc, arguments)
        if fragment:
            self.assertIn(fragment.lower(), str(caught.exception).lower())
        self.assertEqual(self.objects(doc), before, "Rejected specification changed the in-memory PDF")
        self.assertEqual({p: p.read_bytes() for p in self.root.iterdir() if p.is_file()}, files_before)

    def widget(self, doc, name, page=0):
        owner = doc[page]
        widget = next(widget for widget in owner.widgets() if widget.field_name == name)
        return owner, widget

    def appearance(self, doc, index, rect=None, annots=True):
        page = doc[index]
        return page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=rect, annots=annots).samples

    def radio_data(self, doc, name):
        result = []
        for page in doc:
            for widget in page.widgets() or ():
                if widget.field_name != name:
                    continue
                parent = int(doc.xref_get_key(widget.xref, "Parent")[1].split()[0])
                result.append((page.number, doc.xref_get_key(parent, "V"),
                               doc.xref_get_key(widget.xref, "AS"), widget.field_value))
        return result

    def test_radio_cross_page_select_b_then_a_preserves_appearances_and_other_fields(self):
        doc = self.fixture()
        other = self.radio_data(doc, "other")
        before = [self.appearance(doc, index, pymupdf.Rect(25, 55, 55, 85)) for index in range(2)]
        streams = [hashlib.sha256(doc.xref_stream(x)).hexdigest() for x in range(1, doc.xref_length()) if doc.xref_is_stream(x)]
        result = fill_fields(doc, {"pick": "B"})
        self.assertEqual(self.radio_data(doc, "pick"), [(0, ("name", "/B"), ("name", "/Off"), "Off"), (1, ("name", "/B"), ("name", "/B"), "B")])
        self.assertEqual(self.radio_data(doc, "other"), other)
        self.assertEqual(self.widget(doc, "plain")[1].field_value, "Original")
        self.assertEqual(streams, [hashlib.sha256(doc.xref_stream(x)).hexdigest() for x in range(1, doc.xref_length()) if doc.xref_is_stream(x)])
        reopened = self.saved(doc, result)
        after = [self.appearance(reopened, index, pymupdf.Rect(25, 55, 55, 85)) for index in range(2)]
        self.assertNotEqual(before[0], after[0])
        self.assertNotEqual(before[1], after[1])
        self.assertEqual(before[0], after[1], "Selected radio appearance was not transferred")
        self.assertEqual(before[1], after[0], "Unselected radio appearance was not transferred")
        second = fill_fields(reopened, {"pick": "A"})
        restored = self.saved(reopened, second, name="restored.pdf")
        self.assertEqual([self.appearance(restored, index, pymupdf.Rect(25, 55, 55, 85)) for index in range(2)], before)

    def test_radio_export_value_is_explicit_and_unknown_rejected_before_text_mutation(self):
        for value in (True, False, "Off", "C", 1, None, ["A"]):
            with self.subTest(value=value):
                self.reject_atomic(self.fixture(), fill_fields, {"plain": "Changed", "pick": value})

    def test_radio_duplicate_export_values_rejected(self):
        doc = self.fixture()
        owner, widget = self.widget(doc, "pick", 1)
        for key in ("N", "D", "R"):
            value = doc.xref_get_key(widget.xref, "AP/" + key)[1]
            doc.xref_set_key(widget.xref, "AP/" + key, value.replace("/B ", "/A "))
        self.reject_atomic(doc, fill_fields, {"pick": "A"}, "ambiguous")

    def test_independent_radio_groups_with_same_name_rejected(self):
        doc = self.fixture()
        owner, widget = self.widget(doc, "other")
        parent = int(doc.xref_get_key(widget.xref, "Parent")[1].split()[0])
        doc.xref_set_key(parent, "T", "(pick)")
        self.reject_atomic(doc, fill_fields, {"pick": "A"}, "independent")

    def test_radio_readonly_inherited_from_parent(self):
        doc = self.fixture()
        owner, widget = self.widget(doc, "pick")
        parent = int(doc.xref_get_key(widget.xref, "Parent")[1].split()[0])
        doc.xref_set_key(parent, "Ff", str(widget.field_flags | 1))
        self.reject_atomic(doc, fill_fields, {"plain": "Changed", "pick": "B"}, "read-only")

    def test_radio_child_value_override_rejected(self):
        doc = self.fixture()
        owner, widget = self.widget(doc, "pick")
        doc.xref_set_key(widget.xref, "V", "/A")
        self.reject_atomic(doc, fill_fields, {"pick": "B"}, "overrides")

    def test_radio_missing_appearance_stream_rejected(self):
        doc = self.fixture()
        owner, widget = self.widget(doc, "pick")
        doc.xref_set_key(widget.xref, "AP/N/A", "13")
        self.reject_atomic(doc, fill_fields, {"pick": "B"}, "stream")

    def test_radio_bad_parent_and_parent_cycle_rejected(self):
        for bad in ("<< /T (bad) >>", "999999 0 R", "self"):
            with self.subTest(bad=bad):
                doc = self.fixture()
                owner, widget = self.widget(doc, "pick")
                if bad == "self":
                    parent = int(doc.xref_get_key(widget.xref, "Parent")[1].split()[0])
                    doc.xref_set_key(parent, "Parent", f"{parent} 0 R")
                    kids = doc.xref_get_key(parent, "Kids")[1]
                    doc.xref_set_key(parent, "Kids", kids[:-1] + f" {parent} 0 R]")
                else:
                    doc.xref_set_key(widget.xref, "Parent", bad)
                self.reject_atomic(doc, fill_fields, {"plain": "Changed", "pick": "A"})

    def test_radio_escaped_export_names(self):
        doc = self.fixture()
        owner, widget = self.widget(doc, "pick", 1)
        for key in ("N", "D", "R"):
            value = doc.xref_get_key(widget.xref, "AP/" + key)[1]
            doc.xref_set_key(widget.xref, "AP/" + key, value.replace("/B ", "/Alpha#20One "))
        result = fill_fields(doc, {"pick": "Alpha One"})
        reopened = self.saved(doc, result)
        self.assertEqual(self.radio_data(reopened, "pick")[1][1:3], (("name", "/Alpha One"), ("name", "/Alpha One")))

    def test_radio_flattened_appearance_preserved(self):
        doc = self.fixture()
        result = fill_fields(doc, {"pick": "B"})
        expected = [self.appearance(doc, index) for index in range(2)]
        reopened = self.saved(doc, result, flattened=True)
        self.assertEqual([self.appearance(reopened, index) for index in range(2)], expected)
        self.assertTrue(all(not list(page.widgets() or ()) for page in reopened))

    def test_create_all_types_persist_metadata_and_have_visible_appearance(self):
        doc = self.blank()
        result = create_fields(doc, self.specs())
        expected = self.appearance(doc, 0)
        reopened = self.saved(doc, result)
        self.assertEqual(self.appearance(reopened, 0), expected)
        self.assertNotEqual(self.appearance(reopened, 0), self.appearance(reopened, 0, annots=False))
        page = reopened[0]
        fields = {widget.field_name: widget for widget in page.widgets()}
        self.assertEqual(fields["full_name"].field_value, "Ada Example")
        self.assertEqual(fields["full_name"].field_label, "Full name")
        self.assertEqual(fields["full_name"].text_maxlen, 30)
        self.assertEqual(fields["full_name"].field_flags, 2)
        self.assertEqual(fields["agree"].field_value, "Yes")
        self.assertEqual(fields["category"].choice_values, ["Alpha", "Beta"])
        self.assertEqual(fields["region"].field_value, "West")
        self.assertEqual(fields["region"].field_flags, 1)
        self.assertIn("Original page content", page.get_text())
        for spec in self.specs():
            rect = pymupdf.Rect(spec["rect"])
            self.assertNotEqual(self.appearance(reopened, 0, rect), self.appearance(reopened, 0, rect, annots=False), spec["name"])

    def test_create_rotated_coordinates_and_flattened_appearance(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                doc = self.blank(rotation)
                result = create_fields(doc, self.specs())
                expected = self.appearance(doc, 0)
                reopened = self.saved(doc, result, flattened=True, name=f"rotated-{rotation}.pdf")
                self.assertEqual(reopened[0].rotation, rotation)
                self.assertEqual(self.appearance(reopened, 0), expected)
                self.assertFalse(list(reopened[0].widgets() or ()))
                self.assertIn("Ada Example", reopened[0].get_text())

    def test_fill_created_fields_saves_and_checkbox_raster_changes(self):
        doc = self.blank()
        create_fields(doc, self.specs())
        before = self.appearance(doc, 0, pymupdf.Rect(20, 80, 42, 102))
        result = fill_fields(doc, {"full_name": "Changed", "agree": False, "category": "Alpha"})
        reopened = self.saved(doc, result)
        self.assertNotEqual(before, self.appearance(reopened, 0, pymupdf.Rect(20, 80, 42, 102)))
        self.assertEqual(self.widget(reopened, "full_name")[1].field_value, "Changed")
        self.assertEqual(self.widget(reopened, "region")[1].field_value, "West")

    def test_missing_initial_values_and_empty_choices_values_supported(self):
        doc = self.blank()
        specs = self.specs()
        for spec in specs:
            spec.pop("value")
        result = create_fields(doc, specs)
        reopened = self.saved(doc, result)
        self.assertEqual(self.widget(reopened, "full_name")[1].field_value, "")
        self.assertEqual(self.widget(reopened, "category")[1].field_value, "")
        self.assertIn(self.widget(reopened, "agree")[1].field_value, ("Off", ""))

    def test_creation_invalid_specs_atomic(self):
        invalid = [
            {}, {"page": 0}, {"page": True}, {"page": 2}, {"type": "signature"}, {"type": "radio"},
            {"type": []}, {"rect": [1, 2, 3]}, {"rect": [20, 20, float("nan"), 60]},
            {"rect": [20, 20, float("inf"), 60]}, {"rect": [20, 20, True, 60]},
            {"rect": [-1, 20, 200, 60]}, {"rect": [20, 20, 401, 60]},
            {"rect": [20, 20, 20, 60]}, {"rect": [50, 20, 20, 60]},
            {"name": ""}, {"name": "x" * 129}, {"name": "a.b"}, {"name": "a\x00b"},
            {"value": "漢字"}, {"value": "a\nb"}, {"value": "a\x1bb"}, {"value": "a\u202eb"},
            {"value": "x" * 100001}, {"value": True}, {"label": "x" * 257}, {"label": ""},
            {"required": 1}, {"read_only": "false"}, {"font_size": 0}, {"font_size": True},
            {"font_size": 37}, {"font_size": float("nan")}, {"max_length": -1},
            {"max_length": True}, {"max_length": 100001}, {"max_length": 2, "value": "long"},
            {"choices": ["A"]}, {"multiline": True}, {"flags": 8192}, {"script": "unsafe"},
        ]
        base = {"page": 1, "name": "new", "type": "text", "rect": [20, 300, 200, 340]}
        for update in invalid:
            with self.subTest(update=repr(update)[:100]):
                spec = dict(base, **update) if update else {}
                self.reject_atomic(self.blank(), create_fields, [self.specs()[0], spec])

    def test_creation_invalid_choice_and_checkbox_specs(self):
        for update in ({"choices": []}, {"choices": ["A", "A"]}, {"choices": [["a", "A"]]},
                       {"choices": ["漢字"]}, {"value": "Other"}, {"max_length": 2}):
            with self.subTest(update=update):
                self.reject_atomic(self.blank(), create_fields, [dict(self.specs()[2], **update)])
        for update in ({"value": "Yes"}, {"font_size": 12}, {"choices": ["A"]}):
            with self.subTest(update=update):
                self.reject_atomic(self.blank(), create_fields, [dict(self.specs()[1], **update)])

    def test_creation_limits_duplicate_names_and_overlap(self):
        for specs in ([], {}, [self.specs()[0]] * 201, [self.specs()[0], self.specs()[0]],
                      [self.specs()[0], dict(self.specs()[0], name="overlap")]):
            with self.subTest(specs=str(specs)[:100]):
                self.reject_atomic(self.blank(), create_fields, specs)
        doc = self.blank()
        create_fields(doc, [self.specs()[0]])
        self.reject_atomic(doc, create_fields, [dict(self.specs()[0], rect=[20, 300, 260, 340])], "existing")
        self.reject_atomic(doc, create_fields, [dict(self.specs()[0], name="other")], "overlap")

    def test_creation_reserves_nonvisual_acroform_names(self):
        doc = self.blank()
        hidden = doc.get_new_xref()
        doc.update_object(hidden, "<< /FT /Tx /T (hidden) /V (untouched) >>")
        doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"<< /Fields [{hidden} 0 R] >>")
        self.reject_atomic(doc, create_fields, [dict(self.specs()[0], name="hidden")], "existing")

    def test_rotated_outside_unrotated_bounds_rejected(self):
        doc = self.blank(90)
        self.reject_atomic(doc, create_fields, [dict(self.specs()[0], rect=[410, 20, 480, 60])], "unrotated")

    def test_fill_invalid_mapping_and_mixed_edits_atomic(self):
        for value in ({}, [], {"missing": "x"}, {"plain": "Changed", "missing": "x"},
                      {"plain": 4}, {"plain": "漢字"}, {"plain": "a\nb"},
                      {"plain": "a\x00b"}, {"plain": "x" * 101}):
            with self.subTest(value=str(value)[:100]):
                self.reject_atomic(self.fixture(), fill_fields, value)

    def test_fill_readonly_choice_limits_and_checkbox_types(self):
        doc = self.blank()
        create_fields(doc, self.specs())
        for mapping in ({"full_name": "Changed", "region": "East"},
                        {"full_name": "x" * 31}, {"category": "Missing"},
                        {"agree": "Yes"}, {"agree": 1}):
            with self.subTest(mapping=mapping):
                self.reject_atomic(doc, fill_fields, mapping)

    def test_fill_paired_choices_and_multiselect_rejected(self):
        for kind in ("pairs", "multiple"):
            with self.subTest(kind=kind):
                doc = self.blank()
                create_fields(doc, [self.specs()[2]])
                owner, widget = self.widget(doc, "category")
                if kind == "pairs":
                    doc.xref_set_key(widget.xref, "Opt", "[[(a)(Alpha)] [(b)(Beta)]]")
                else:
                    doc.xref_set_key(widget.xref, "Ff", str(widget.field_flags | (1 << 21)))
                self.reject_atomic(doc, fill_fields, {"category": "Alpha"}, "not supported")

    def test_fill_existing_multiline_supported_but_password_and_richtext_rejected(self):
        doc = self.blank()
        create_fields(doc, [self.specs()[0]])
        owner, widget = self.widget(doc, "full_name")
        doc.xref_set_key(widget.xref, "Ff", str(1 << 12))
        result = fill_fields(doc, {"full_name": "Line one\nLine two"})
        reopened = self.saved(doc, result)
        self.assertEqual(self.widget(reopened, "full_name")[1].field_value, "Line one\nLine two")
        for flag in (1 << 13, 1 << 20, 1 << 25):
            with self.subTest(flag=flag):
                doc = self.blank()
                create_fields(doc, [self.specs()[0]])
                owner, widget = self.widget(doc, "full_name")
                doc.xref_set_key(widget.xref, "Ff", str(flag))
                self.reject_atomic(doc, fill_fields, {"full_name": "Text"}, "not supported")

    def test_inherited_signature_and_readonly_text_rejected(self):
        for fieldtype, flags in (("Sig", 0), ("Tx", 1)):
            with self.subTest(fieldtype=fieldtype):
                doc = self.blank()
                create_fields(doc, [self.specs()[0]])
                owner, widget = self.widget(doc, "full_name")
                parent = doc.get_new_xref()
                doc.update_object(parent, f"<< /FT /{fieldtype} /T (inherited) /Ff {flags} /Kids [{widget.xref} 0 R] >>")
                for key in ("T", "FT", "Ff"):
                    doc.xref_set_key(widget.xref, key, "null")
                doc.xref_set_key(widget.xref, "Parent", f"{parent} 0 R")
                doc.xref_set_key(doc.pdf_catalog(), "AcroForm/Fields", f"[{parent} 0 R]")
                self.reject_atomic(doc, fill_fields, {"inherited": "Text"}, "Signature" if fieldtype == "Sig" else "read-only")

    def test_verifier_detects_persisted_value_and_state_changes(self):
        doc = self.fixture()
        result = fill_fields(doc, {"pick": "B"})
        owner, widget = self.widget(doc, "pick", 1)
        doc.xref_set_key(widget.xref, "AS", "/Off")
        path = self.root / "tampered.pdf"
        doc.save(path)
        with self.assertRaisesRegex(JobError, "verification failed"):
            verify_forms(path, result["forms_expected"])
        with self.assertRaisesRegex(JobError, "flattened widgets remain"):
            verify_forms(path, result["forms_expected"], flattened=True)


if __name__ == "__main__":
    unittest.main()
