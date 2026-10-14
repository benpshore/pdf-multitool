"""Real MuPDF regressions for links in merge and grouped page extraction."""
from pathlib import Path
import tempfile
import unittest

import pymupdf

from pdf_multitool.core import Job


class PageLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "linked.pdf"
        with pymupdf.open() as doc:
            for index in range(3):
                doc.new_page().insert_text((40, 80), f"Source page {index + 1}")
            for origin, target, y in [(0, 2, 100), (2, 0, 100), (0, 1, 140)]:
                doc[origin].insert_link({"kind": pymupdf.LINK_GOTO,
                                         "from": pymupdf.Rect(40, y, 180, y + 20),
                                         "page": target, "to": pymupdf.Point(40, 80)})
            doc[0].insert_link({"kind": pymupdf.LINK_URI,
                                "from": pymupdf.Rect(40, 180, 180, 200),
                                "uri": "https://example.invalid/synthetic"})
            widget = pymupdf.Widget()
            widget.field_name = "Synthetic field"
            widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
            widget.rect = pymupdf.Rect(40, 220, 180, 240)
            widget.field_value = "Retain me"
            doc[0].add_widget(widget)
            doc.save(self.source)
        self.original = self.source.read_bytes()

    def tearDown(self):
        self.assertEqual(self.source.read_bytes(), self.original)
        self.temp.cleanup()

    def run_job(self, operation, inputs=None, options=None):
        files = [{"id": str(i), "name": p.name, "path": str(p)}
                 for i, p in enumerate(inputs or [self.source])]
        job = Job(operation, files, options or {}, self.root / "out")
        job.run()
        self.assertEqual(job.state, "completed", job.errors)
        return job

    def assert_links(self, output, page, destinations, uri=False, widget=False):
        with pymupdf.open(output) as doc:
            links = doc[page].get_links()
            self.assertEqual(sorted(link["page"] for link in links if link["kind"] == pymupdf.LINK_GOTO), destinations)
            for link in links:
                if link["kind"] == pymupdf.LINK_GOTO:
                    self.assertEqual(link["to"], pymupdf.Point(40, 80))
            self.assertEqual([link["uri"] for link in links if link["kind"] == pymupdf.LINK_URI],
                             ["https://example.invalid/synthetic"] if uri else [])
            if widget:
                self.assertEqual([w.field_value for w in doc[page].widgets()], ["Retain me"])

    def test_merge_preserves_cross_page_links_and_offsets(self):
        job = self.run_job("merge", inputs=[self.source, self.source])
        output = job.results[0]["path"]
        self.assert_links(output, 0, [1, 2], uri=True, widget=True)
        self.assert_links(output, 2, [0])
        self.assert_links(output, 3, [4, 5], uri=True, widget=True)
        self.assert_links(output, 5, [3])

    def test_merge_reordered_subset_remaps_links_and_removes_omitted_targets(self):
        job = self.run_job("merge", options={"pages": "3,1,3"})
        output = job.results[0]["path"]
        self.assert_links(output, 0, [1])
        self.assert_links(output, 1, [0], uri=True, widget=True)
        with pymupdf.open(output) as doc:
            self.assertEqual(len(doc), 2)
            self.assertIn("Source page 3", doc[0].get_text())

    def test_split_groups_keep_independent_link_maps(self):
        job = self.run_job("split", options={"pages": "1-3;3,1;1"})
        self.assert_links(job.results[0]["path"], 0, [1, 2], uri=True, widget=True)
        self.assert_links(job.results[1]["path"], 0, [1])
        self.assert_links(job.results[1]["path"], 1, [0], uri=True, widget=True)
        self.assert_links(job.results[2]["path"], 0, [], uri=True, widget=True)

    def test_split_encrypted_source_with_password(self):
        encrypted = self.root / "encrypted.pdf"
        with pymupdf.open(self.source) as doc:
            doc.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                     owner_pw="synthetic-owner", user_pw="synthetic-user")
        job = self.run_job("split", inputs=[encrypted], options={"pages": "3,1;1-3", "password": "synthetic-user"})
        self.assert_links(job.results[0]["path"], 1, [0], uri=True, widget=True)
        self.assert_links(job.results[1]["path"], 0, [1, 2], uri=True, widget=True)

    def test_cross_page_link_coordinates_with_rotations_and_mixed_page_sizes(self):
        rotated = self.root / "rotated-links.pdf"
        with pymupdf.open() as doc:
            doc.new_page(width=400, height=600)
            doc.new_page(width=800, height=300)
            doc[0].insert_link({"kind": pymupdf.LINK_GOTO,
                                "from": pymupdf.Rect(30, 50, 170, 80),
                                "page": 1, "to": pymupdf.Point(120, 90)})
            doc[0].insert_link({"kind": pymupdf.LINK_GOTO,
                                "from": pymupdf.Rect(30, 100, 170, 130),
                                "page": 0, "to": pymupdf.Point(40, 150)})
            doc[0].insert_link({"kind": pymupdf.LINK_URI,
                                "from": pymupdf.Rect(30, 160, 170, 190),
                                "uri": "https://example.invalid/rotated"})
            doc[0].set_rotation(90)
            doc[1].set_rotation(270)
            doc.save(rotated)
        with pymupdf.open(rotated) as doc:
            original_links = doc[0].get_links()
        for operation in ("merge", "split"):
            with self.subTest(operation=operation):
                inputs = [rotated, rotated] if operation == "merge" else [rotated]
                job = self.run_job(operation, inputs=inputs, options={"pages": "2,1"})
                with pymupdf.open(job.results[0]["path"]) as doc:
                    if operation == "merge":
                        duplicate_links = doc[3].get_links()
                        self.assertEqual(duplicate_links[0]["page"], 2)
                        self.assertEqual(duplicate_links[1]["page"], 3)
                        self.assertEqual(duplicate_links[2]["from"], original_links[2]["from"])
                    links = doc[1].get_links()
                    self.assertEqual(len(links), 3)
                    self.assertEqual(links[0]["page"], 0)
                    self.assertEqual(links[1]["page"], 1)
                    for actual, expected in zip(links, original_links):
                        self.assertEqual(actual["kind"], expected["kind"])
                        self.assertEqual(actual["from"], expected["from"])
                        if expected["kind"] == pymupdf.LINK_GOTO:
                            self.assertEqual(actual["to"], expected["to"])
                        else:
                            self.assertEqual(actual["uri"], expected["uri"])

    def test_remote_and_launch_links_survive_copy_without_following_them(self):
        external = self.root / "external-links.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_link({"kind": pymupdf.LINK_GOTOR,
                              "from": pymupdf.Rect(20, 30, 160, 50),
                              "file": "synthetic-other.pdf", "page": 2,
                              "to": pymupdf.Point(70, 80), "zoom": 1.5})
            page.insert_link({"kind": pymupdf.LINK_LAUNCH,
                              "from": pymupdf.Rect(20, 90, 160, 110),
                              "file": "synthetic.txt"})
            page.set_rotation(90)
            doc.save(external)
        with pymupdf.open(external) as doc:
            expected = doc[0].get_links()
        job = self.run_job("merge", inputs=[self.source, external, external])
        with pymupdf.open(job.results[0]["path"]) as doc:
            for page in (3, 4):
                actual = doc[page].get_links()
                self.assertEqual(len(actual), 2)
                for found, original in zip(actual, expected):
                    for key in ("kind", "from", "file", "page", "to", "zoom"):
                        self.assertEqual(found.get(key), original.get(key))
