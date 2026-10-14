"""Bounded PDF action preservation and explicit omission warnings; no links followed."""
from pathlib import Path
import tempfile
import unittest

import pymupdf
from pdf_multitool.core import Job


class LinkActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_source(self, actions, rotation=0, same_rect=False, cropped=False):
        path = self.root / "actions.pdf"
        with pymupdf.open() as doc:
            doc.new_page(width=400,height=600)
            doc.new_page(width=400,height=600)
            for i, action in enumerate(actions):
                page = doc[0]
                y = 30 if same_rect else 30 + 45*i
                page.insert_link({"kind":pymupdf.LINK_URI,"from":pymupdf.Rect(20,y,170,y+20),"uri":"https://example.invalid/placeholder"})
                doc.xref_set_key(page.annot_xrefs()[-1][0], "A", action)
            if cropped:
                doc[0].set_cropbox(pymupdf.Rect(10,10,380,580))
            doc[0].set_rotation(rotation)
            doc.save(path)
        return path

    def run_copy(self, path, operation="merge", pages=""):
        original = path.read_bytes()
        job = Job(operation,[{"id":"s","name":path.name,"path":str(path)}],{"pages":pages},self.root/"jobs")
        job.run()
        self.assertEqual(job.state,"completed",job.errors)
        self.assertEqual(path.read_bytes(),original)
        return job

    def test_named_remote_string_and_name_destinations_keep_raw_actions(self):
        filename = "synthetic (remote) ü.pdf"
        actions = ["<< /S /GoToR /F " + pymupdf.get_pdf_str(filename) + " /D " + target + " >>" for target in [pymupdf.get_pdf_str("A named ü target"),"/Named#20Target"]]
        path = self.make_source(actions,rotation=90)
        with pymupdf.open(path) as source:
            expected = [[source.xref_get_key(row[0],key) for key in ("A/S","A/F","A/D")] for row in source[0].annot_xrefs()]
            rectangles = [link["from"] for link in source[0].get_links()]
        for operation in ("merge","split"):
            job = self.run_copy(path,operation,"2,1")
            with pymupdf.open(job.results[0]["path"]) as output:
                actual = [[output.xref_get_key(row[0],key) for key in ("A/S","A/F","A/D")] for row in output[1].annot_xrefs()]
                self.assertEqual(actual,expected)
                self.assertEqual([link["from"] for link in output[1].get_links()],rectangles)
            self.assertFalse(any("unsupported" in warning for warning in job.warnings))

    def test_unknown_and_named_actions_warn_without_rewriting_them(self):
        actions = ["<< /S /JavaScript /JS (synthetic) >>", "<< /S /Named /N /NextPage >>", "<< /S /URI /URI (https://example.invalid/retained) >>"]
        for rotation in (0,90,180,270):
            for cropped in (False,True):
                with self.subTest(rotation=rotation,cropped=cropped):
                    path = self.make_source(actions,rotation,cropped=cropped)
                    with pymupdf.open(path) as source:
                        expected = [link["from"] for link in source[0].get_links() if link.get("uri")][0]
                    job = self.run_copy(path)
                    with pymupdf.open(job.results[0]["path"]) as output:
                        links = output[0].get_links()
                        self.assertEqual(len(links),1)
                        self.assertEqual(links[0]["uri"],"https://example.invalid/retained")
                        self.assertEqual(links[0]["from"],expected)
                    self.assertTrue(any("2 unsupported or ambiguous" in warning for warning in job.warnings),job.warnings)

    def test_ambiguous_rectangles_are_not_guessed(self):
        path = self.make_source(["<< /S /JavaScript /JS (synthetic) >>","<< /S /URI /URI (https://example.invalid) >>"],same_rect=True)
        job = self.run_copy(path)
        with pymupdf.open(job.results[0]["path"]) as output:
            self.assertEqual(output[0].get_links(),[])
        self.assertTrue(any("2 unsupported or ambiguous" in warning for warning in job.warnings))

    def test_chained_action_is_explicitly_omitted(self):
        path = self.make_source(["<< /S /URI /URI (https://example.invalid) /Next << /S /JavaScript /JS (synthetic) >> >>"])
        job = self.run_copy(path)
        with pymupdf.open(job.results[0]["path"]) as output:
            self.assertEqual(output[0].get_links(),[])
        self.assertTrue(any("1 unsupported or ambiguous" in warning for warning in job.warnings))

    def test_omitted_page_targets_get_a_warning(self):
        path = self.make_source([])
        with pymupdf.open(path) as doc:
            doc[0].insert_link({"kind":pymupdf.LINK_GOTO,"from":pymupdf.Rect(20,30,170,50),"page":1,"to":pymupdf.Point(10,20)})
            other = self.root / "target.pdf"
            doc.save(other)
        job = self.run_copy(other,"split","1")
        self.assertTrue(any("1 internal link(s)" in warning and "omitted" in warning for warning in job.warnings))

    def test_cropped_rotated_internal_targets_keep_coordinates(self):
        for rotation in (90,180,270):
            path = self.root / "crop-links.pdf"
            with pymupdf.open() as doc:
                for _ in range(2):
                    doc.new_page(width=500,height=650)
                doc[0].insert_link({"kind":pymupdf.LINK_GOTO,"from":pymupdf.Rect(70,70,180,90),"page":1,"to":pymupdf.Point(100,120)})
                doc[0].set_cropbox(pymupdf.Rect(40,40,450,600))
                doc[1].set_cropbox(pymupdf.Rect(20,30,440,590))
                doc[0].set_rotation(rotation)
                doc[1].set_rotation((rotation+90)%360)
                doc.save(path)
            with pymupdf.open(path) as source:
                expected = source[0].get_links()[0]
            for operation in ("merge","split"):
                job = self.run_copy(path,operation,"2,1")
                with pymupdf.open(job.results[0]["path"]) as output:
                    actual = output[1].get_links()[0]
                    self.assertEqual(actual["page"],0)
                    self.assertEqual(actual["from"],expected["from"])
                    self.assertEqual(actual["to"],expected["to"])
