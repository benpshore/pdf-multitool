"""Synthetic real-engine regressions for copying multi-page radio groups."""
import io
from pathlib import Path
import tempfile
import unittest

import pymupdf
from reportlab.pdfgen.canvas import Canvas

from pdf_multitool.core import Job
from pdf_multitool.forms import fill_fields


class CrossPageFormTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "radio.pdf"
        buf = io.BytesIO()
        canvas = Canvas(buf, pagesize=(400, 500))
        form = canvas.acroForm
        form.radio(name="pick", value="A", x=30, y=420, selected=True)
        canvas.showPage()
        form.radio(name="pick", value="B", x=30, y=420)
        canvas.showPage()
        canvas.save()
        self.original = buf.getvalue()
        self.source.write_bytes(self.original)

    def tearDown(self):
        self.assertEqual(self.source.read_bytes(), self.original)
        self.tmp.cleanup()

    def run_job(self, operation, pages="", paths=None):
        paths = paths or [self.source]
        job = Job(operation, [{"id":str(i), "name":p.name, "path":str(p)} for i,p in enumerate(paths)], {"pages":pages}, self.root / "jobs")
        job.run()
        return job

    def test_full_group_merge_and_split_preserve_widgets_and_fillability(self):
        for operation in ("merge", "split"):
            with self.subTest(operation=operation):
                job = self.run_job(operation, "1-2")
                self.assertEqual(job.state, "completed", job.errors)
                with pymupdf.open(job.results[0]["path"]) as doc:
                    self.assertEqual([len(list(p.widgets() or ())) for p in doc], [1,1])
                    fill_fields(doc, {"pick":"B"})
                    saved = self.root / f"filled-{operation}.pdf"
                    doc.save(saved)
                with pymupdf.open(saved) as doc:
                    self.assertEqual([next(p.widgets()).field_value for p in doc], ["Off","B"])

    def test_partial_and_reordered_groups_fail_without_output(self):
        for operation, pages in [("split",""),("split","1"),("split","1-2;1"),("split","2,1"),("merge","1"),("merge","2,1")]:
            with self.subTest(operation=operation,pages=pages):
                job = self.run_job(operation, pages)
                self.assertEqual(job.state, "failed", job.errors)
                self.assertFalse(job.results)
                self.assertIn("radio-button group spans multiple pages", str(job.errors))
                self.assertFalse(list(job.directory.glob("**/*.pdf")))

    def test_repeated_names_keep_all_widgets_or_fail_closed(self):
        for field_type in ("text", "checkbox"):
            with self.subTest(field_type=field_type):
                buf = io.BytesIO()
                canvas = Canvas(buf, pagesize=(400,500))
                for i in range(2):
                    if field_type == "text":
                        canvas.acroForm.textfield(name="same", x=30,y=420,width=120,height=20,value=str(i))
                    else:
                        canvas.acroForm.checkbox(name="same", x=30,y=420,checked=i==0)
                    canvas.showPage()
                canvas.save()
                path = self.root / (field_type + ".pdf")
                path.write_bytes(buf.getvalue())
                job = self.run_job("merge", "1-2", [path])
                self.assertEqual(job.state,"completed",job.errors)
                with pymupdf.open(job.results[0]["path"]) as doc:
                    widgets = [list(page.widgets() or ()) for page in doc]
                    self.assertEqual([len(row) for row in widgets],[1,1])
                    self.assertEqual([row[0].field_value for row in widgets], ["0","1"] if field_type=="text" else ["Yes","Off"])
                    names = [row[0].field_name for row in widgets]
                    self.assertEqual(len(set(names)),2)
                    fill_fields(doc, {name: "Updated" if field_type=="text" else True for name in names})
                    filled = self.root / (field_type + "-filled.pdf")
                    doc.save(filled)
                with pymupdf.open(filled) as doc:
                    self.assertEqual([next(page.widgets()).field_value for page in doc], ["Updated","Updated"] if field_type=="text" else ["Yes","Yes"])
                self.assertTrue(any("renamed" in warning for warning in job.warnings))
                job = self.run_job("split", "2,1", [path])
                self.assertEqual(job.state,"failed",job.errors)
                self.assertFalse(job.results)
                self.assertIn("form field occurs on multiple selected pages", str(job.errors))

    def test_duplicate_radio_inputs_preserve_separate_groups(self):
        job = self.run_job("merge", paths=[self.source,self.source])
        self.assertEqual(job.state,"completed",job.errors)
        with pymupdf.open(job.results[0]["path"]) as doc:
            self.assertEqual([len(list(p.widgets() or ())) for p in doc],[1,1,1,1])
            names = [next(doc[i].widgets()).field_name for i in (0,2)]
            self.assertNotEqual(*names)
            fill_fields(doc,{names[1]:"B"})
            saved = self.root / "two-groups.pdf"
            doc.save(saved)
        with pymupdf.open(saved) as doc:
            self.assertEqual([next(p.widgets()).field_value for p in doc],["A","Off","Off","B"])
        self.assertTrue(any("renamed" in warning for warning in job.warnings))
