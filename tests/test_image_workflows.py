"""Actual supervised workers and HTTP/CLI intake; fixtures contain no user data."""
import io
import gzip
from contextlib import redirect_stderr
import json
from pathlib import Path
import subprocess
import sys
import unittest
import threading

import pymupdf
from PIL import Image

from pdf_multitool.core import Job, Limits, run_worker
from tests import test_archive_workflows as workflow_helpers
from tests.fixtures import document


class ImageWorkflowTests(unittest.TestCase):
    setUp = workflow_helpers.ArchiveWorkflowTests.setUp
    tearDown = workflow_helpers.ArchiveWorkflowTests.tearDown
    request = workflow_helpers.ArchiveWorkflowTests.request
    upload = workflow_helpers.ArchiveWorkflowTests.upload
    zip = workflow_helpers.ArchiveWorkflowTests.zip
    wait_job = workflow_helpers.ArchiveWorkflowTests.wait_job
    process = workflow_helpers.ArchiveWorkflowTests.process

    def photo(self, format='PNG', color='red', size=(120, 80)):
        buffer = io.BytesIO()
        Image.new('RGB', size, color).save(buffer, format=format)
        return buffer.getvalue()

    def test_actual_codecs_upload_convert_and_preserve_originals(self):
        files = []
        for name, format, color in [('photo one.jpg', 'JPEG', 'red'), ('photo.png', 'PNG', 'green'), ('photo.jp2', 'JPEG2000', 'blue')]:
            original = self.photo(format, color)
            file = self.upload(name, original)
            files.append(file)
            status, saved = self.request('GET', f"/api/files/{file['id']}/original")
            self.assertEqual((status, saved), (200, original))
        job = self.process('imagepdf', [f['id'] for f in files], {'dpi': 144})
        self.assertEqual(job['state'], 'completed', job)
        _, data = self.request('GET', job['results'][0]['url'])
        with pymupdf.open(stream=data, filetype='pdf') as doc:
            self.assertEqual(len(doc), 3)
            for i, page in enumerate(doc):
                self.assertEqual(tuple(page.rect), (0, 0, 60, 40))
                self.assertEqual(page.get_text(), '')
                pixmap = page.get_pixmap()
                sample = pixmap.pixel(pixmap.width // 2, pixmap.height // 2)
                self.assertEqual(sample.index(max(sample)), i)
        self.assertTrue(any('OCR' in w for w in job['warnings']))
        self.assertTrue(any('photo one.jpg:' in w for w in job['warnings']))
        self.assertFalse(any(f['id'] in w for f in files for w in job['warnings']))

    def test_mixed_pdf_image_merge_preserves_order(self):
        first = self.upload('before.pdf', document(self.root / 'before.pdf', 1).read_bytes())
        middle = self.upload('middle.png', self.photo())
        last = self.upload('after.pdf', document(self.root / 'after.pdf', 2).read_bytes())
        job = self.process('merge', [first['id'], middle['id'], last['id']], {'dpi': 72})
        self.assertEqual(job['state'], 'completed', job)
        _, data = self.request('GET', job['results'][0]['url'])
        with pymupdf.open(stream=data, filetype='pdf') as doc:
            self.assertEqual(len(doc), 4)
            self.assertTrue(doc[0].get_text())
            self.assertEqual(doc[1].get_text(), '')
            self.assertEqual(tuple(doc[1].rect), (0, 0, 120, 80))
            self.assertTrue(doc[2].get_text())
            self.assertTrue(doc[3].get_text())

    def test_archive_image_members_and_unknown_formats(self):
        imported = self.upload('photos.zip', self.zip([('nested/photo.png', self.photo()), ('second.jp2', self.photo('JPEG2000')), ('unsupported.heic', b'fakeheic'), ('mismatch.jpg', self.photo())]))
        self.assertEqual(len(imported['files']), 2)
        self.assertEqual(len(imported['warnings']), 2)
        job = self.process('imagepdf', [f['id'] for f in imported['files']])
        self.assertEqual(job['state'], 'completed', job)
        _, data = self.request('GET', job['results'][0]['url'])
        with pymupdf.open(stream=data, filetype='pdf') as doc:
            self.assertEqual(len(doc), 2)

    def test_image_analysis_preview_and_pdf_operation_guidance(self):
        file = self.upload('photo.png', self.photo(size=(120, 80)))
        job = self.process('identify', [file['id']])
        self.assertEqual(job['state'], 'completed', job)
        _, content = self.request('GET', job['results'][0]['url'])
        report = json.loads(content)
        self.assertEqual(report['input_type'], 'image')
        self.assertEqual(report['name'], 'photo.png')
        self.assertEqual(report['width'], 120)
        self.assertEqual(report['native_text_characters'], 0)
        self.assertTrue(report['ocr_candidate'])
        self.assertFalse(report['ocr_performed'])
        status, content = self.request('GET', f"/api/files/{file['id']}/preview")
        self.assertEqual(status, 200, content)
        with Image.open(io.BytesIO(content)) as preview:
            self.assertLessEqual(preview.width, 240)
            self.assertLessEqual(preview.height, 300)
        pdf = self.upload('native.pdf', self.pdf)
        job = self.process('text', [pdf['id'], file['id']])
        self.assertEqual(job['state'], 'partial', job)
        self.assertEqual(len(job['results']), 1)
        self.assertIn('Convert to PDF first', job['errors'][0]['error'])

    def test_invalid_image_fails_atomic_combination(self):
        good = self.upload('good.png', self.photo())
        bad = self.upload('bad.png', b'\x89PNG\r\n\x1a\ntruncated')
        for operation in ['imagepdf', 'merge']:
            job = self.process(operation, [good['id'], bad['id']])
            self.assertEqual(job['state'], 'failed', job)
            self.assertEqual(job['results'], [])
            self.assertEqual(list(self.app.jobs[job['id']].directory.iterdir()), [])

    def test_cli_folder_and_archive_image_inputs(self):
        folder = self.root / 'photos'
        folder.mkdir()
        (folder / '01 photo.png').write_bytes(self.photo())
        (folder / '02 photo.jpg').write_bytes(self.photo('JPEG'))
        command = [sys.executable, '-m', 'pdf_multitool', 'imagepdf', str(folder), '--dpi', '72', '-o', str(self.root / 'cli-results')]
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = json.loads(result.stdout)
        with pymupdf.open(report['results'][0]['path']) as doc:
            self.assertEqual(len(doc), 2)
            self.assertEqual(doc[0].rect.width, 120)
        archive = self.root / 'photos.zip'
        archive.write_bytes(self.zip([('photo.png', self.photo())]))
        command[3] = 'imagepdf'
        command[4] = str(archive)
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)['state'], 'completed')

    def test_cancelled_image_job_produces_no_outputs(self):
        path = self.root / 'cancel.png'
        path.write_bytes(self.photo())
        job = Job('imagepdf', [{'id': 'one', 'name': path.name, 'path': str(path)}], {}, self.root / 'cancel-output')
        job.cancel_event.set()
        job.run()
        self.assertEqual(job.state, 'cancelled')
        self.assertEqual(job.results, [])
        self.assertEqual(list(job.directory.iterdir()), [])

    def test_maximum_image_batch_runs_with_bounded_worker_descriptors(self):
        path = self.root / 'tiny.png'
        path.write_bytes(self.photo(size=(8, 8)))
        count = Limits().max_files
        staging = self.root / 'large-count-worker'
        report = run_worker({'operation': 'imagepdf', 'inputs': [str(path)] * count,
                             'names': ['tiny.png'] * count, 'options': {}},
                            staging, threading.Event())
        self.assertTrue(report['ok'], report)
        with pymupdf.open(staging / report['outputs'][0]['file']) as doc:
            self.assertEqual(len(doc), count)

    def test_long_gzip_image_name_retains_format(self):
        name = 'a' * 210 + '.png.gz'
        imported = self.upload(name, gzip.compress(self.photo()))
        self.assertEqual(len(imported['files']), 1)
        file = imported['files'][0]
        self.assertTrue(file['name'].endswith('.png'))
        job = self.process('imagepdf', [file['id']])
        self.assertEqual(job['state'], 'completed', job)

    def test_running_image_job_cancels_and_removes_partial_outputs(self):
        path = self.root / 'noise.png'
        with Image.effect_noise((1200, 1200), 50) as image:
            image.save(path)
        files = [{'id': str(i), 'name': path.name, 'path': str(path)} for i in range(64)]
        job = Job('imagepdf', files, {}, self.root / 'running-cancel')
        timer = threading.Timer(0.2, job.cancel_event.set)
        timer.start()
        try:
            job.run()
        finally:
            timer.cancel()
        self.assertEqual(job.state, 'cancelled', job.snapshot())
        self.assertEqual(job.results, [])
        self.assertEqual(list(job.directory.iterdir()), [])

    def test_image_cli_rejects_inapplicable_page_and_password_flags(self):
        from pdf_multitool.cli import parser
        for flag in ['--pages=2', '--ask-password']:
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                parser().parse_args(['imagepdf', 'photo.png', flag])
            self.assertEqual(result.exception.code, 2)
