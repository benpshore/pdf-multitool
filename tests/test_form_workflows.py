"""Real CLI/HTTP form paths and encryption disclosures with synthetic fixtures."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest

import pymupdf
from reportlab.pdfgen import canvas

from pdf_multitool.core import Job, JobError, strict_json_loads, read_local_json
from tests import test_archive_workflows as helpers


class FormWorkflowTests(unittest.TestCase):
    setUp = helpers.ArchiveWorkflowTests.setUp
    tearDown = helpers.ArchiveWorkflowTests.tearDown
    request = helpers.ArchiveWorkflowTests.request
    upload = helpers.ArchiveWorkflowTests.upload
    wait_job = helpers.ArchiveWorkflowTests.wait_job
    process = helpers.ArchiveWorkflowTests.process

    def specs(self):
        return [
            {'page': 1, 'name': 'Name', 'type': 'text', 'rect': [40, 100, 280, 125], 'value': 'Synthetic Example', 'max_length': 40},
            {'page': 1, 'name': 'Agreed', 'type': 'checkbox', 'rect': [40, 150, 60, 170], 'value': True},
            {'page': 1, 'name': 'Choice', 'type': 'combobox', 'rect': [40, 190, 240, 216], 'choices': ['Alpha', 'Beta'], 'value': 'Beta'},
            {'page': 2, 'name': 'List', 'type': 'listbox', 'rect': [40, 100, 240, 155], 'choices': ['First', 'Second'], 'value': 'Second'},
        ]

    def pdf_result(self, job):
        self.assertEqual(job['state'], 'completed', job)
        output = next(item for item in job['results'] if item['name'].endswith('.pdf'))
        status, content = self.request('GET', output['url'])
        self.assertEqual(status, 200)
        return content

    def test_create_real_fields_saved_values_appearances_and_clear_options(self):
        file = self.upload('blank.pdf', self.pdf)
        job = self.process('forms', [file['id']], {'create_fields': self.specs()})
        result = self.pdf_result(job)
        with pymupdf.open(stream=result, filetype='pdf') as doc:
            values = {w.field_name: w.field_value for p in doc for w in p.widgets() or []}
            self.assertEqual(values, {'Name': 'Synthetic Example', 'Agreed': 'Yes', 'Choice': 'Beta', 'List': 'Second'})
            self.assertGreater(len(doc[0].get_pixmap().samples), 100)
        self.assertNotIn('create_fields', self.app.jobs[job['id']].options)
        self.assertNotIn('Synthetic Example', json.dumps(job))
        changes = next(item for item in job['results'] if item['name'].endswith('-changes.json'))
        _, detail = self.request('GET', changes['url'])
        self.assertNotIn(b'Synthetic Example', detail)
        self.assertNotIn(b'forms_expected', detail)
        _, original = self.request('GET', f"/api/files/{file['id']}/original")
        self.assertEqual(original, self.pdf)

    def test_create_flatten_preserves_visible_content(self):
        file = self.upload('source.pdf', self.pdf)
        job = self.process('forms', [file['id']], {'create_fields': self.specs(), 'flatten': True})
        with pymupdf.open(stream=self.pdf_result(job), filetype='pdf') as doc:
            self.assertFalse(any(list(p.widgets() or []) for p in doc))
            self.assertIn('Synthetic Example', ''.join(p.get_text() for p in doc))

    def test_radio_export_value_via_api_and_inspection(self):
        path = self.root / 'radio.pdf'
        drawing = canvas.Canvas(str(path))
        drawing.acroForm.radio(name='Delivery', value='Mail', x=40, y=700, selected=True)
        drawing.acroForm.radio(name='Delivery', value='Pickup', x=80, y=700, selected=False)
        drawing.showPage(); drawing.save()
        file = self.upload(path.name, path.read_bytes())
        job = self.process('forms', [file['id']], {'fields': {'Delivery': 'Pickup'}})
        with pymupdf.open(stream=self.pdf_result(job), filetype='pdf') as doc:
            states = [doc.xref_get_key(w.xref, 'AS')[1] for w in doc[0].widgets()]
            self.assertEqual(states, ['/Off', '/Pickup'])
        self.assertNotIn('fields', self.app.jobs[job['id']].options)
        audit = self.process('forms', [file['id']])
        output = next(item for item in audit['results'] if item['name'].endswith('-forms.json'))
        _, data = self.request('GET', output['url'])
        self.assertEqual({f['export_value'] for f in json.loads(data)['fields']}, {'Mail', 'Pickup'})

    def test_invalid_creation_roots_and_later_specs_fail_cleanly(self):
        file = self.upload('source.pdf', self.pdf)
        for creation in [None, {}, [], [self.specs()[0], {'page': 1, 'name': 'Sign', 'type': 'signature', 'rect': [30, 300, 180, 340]}]]:
            with self.subTest(creation=creation):
                job = self.process('forms', [file['id']], {'create_fields': creation})
                self.assertEqual(job['state'], 'failed', job)
                self.assertEqual(job['results'], [])
                internal = self.app.jobs[job['id']]
                self.assertEqual(list(internal.directory.rglob('*')), [])
                self.assertNotIn('create_fields', internal.options)
        job = self.process('forms', [file['id']], {'fields': {}, 'create_fields': self.specs()})
        self.assertEqual(job['state'], 'failed', job)

    def test_public_json_boundaries_reject_duplicate_nonfinite_and_deep(self):
        for value in ['{"a":1,"a":2}', '{"a":NaN}', '[' * 1100 + '0' + ']' * 1100, '[' * 66 + '0' + ']' * 66]:
            with self.subTest(value=value[:40]), self.assertRaises(JobError):
                strict_json_loads(value)
            status, body = self.request('POST', '/api/jobs', value.encode(), {'Content-Type': 'application/json'})
            self.assertEqual(status, 400, body)
            self.assertIn('error', json.loads(body))

    def test_local_json_regular_file_and_byte_limits(self):
        good = self.root / 'fields.json'
        good.write_text('[{"name":"Sample"}]')
        self.assertEqual(read_local_json(good), [{'name': 'Sample'}])
        with self.assertRaisesRegex(JobError, 'too large'):
            read_local_json(good, maximum=2)
        fifo = self.root / 'pipe.json'; os.mkfifo(fifo)
        with self.assertRaisesRegex(JobError, 'regular local'):
            read_local_json(fifo)
        linked = self.root / 'linked.json'; linked.symlink_to(good)
        with self.assertRaises(OSError):
            read_local_json(linked)
        malformed = self.root / 'malformed.json'; malformed.write_text('{')
        with self.assertRaises(ValueError):
            read_local_json(malformed)

    def test_cli_creation_and_wrong_root_inputs(self):
        config = self.root / 'fields.json'; config.write_text(json.dumps(self.specs()))
        source = self.root / 'cli-source.pdf'; source.write_bytes(self.pdf)
        command = [sys.executable, '-m', 'pdf_multitool', 'forms', str(source), '--create-fields-json', str(config), '-o', str(self.root / 'cli-output')]
        done = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        report = json.loads(done.stdout)
        output = next(item for item in report['results'] if item['name'].endswith('.pdf'))
        with pymupdf.open(output['path']) as doc:
            self.assertEqual(sum(len(list(p.widgets() or [])) for p in doc), 4)
        for value in ['null', '{}', '{"a":1,"a":2}', '[' * 1100 + '0' + ']' * 1100]:
            config.write_text(value)
            failed = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
            self.assertNotIn('Traceback', failed.stdout + failed.stderr)

    def test_merge_encryption_warning_and_text_integrity(self):
        for password in ['', 'Synthetic-pw-740284']:
            path = self.root / ('protected-empty.pdf' if not password else 'protected.pdf')
            with pymupdf.open(stream=self.pdf, filetype='pdf') as doc:
                original_text = [p.get_text() for p in doc]
                doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw='Owner-only-1234')
            first = self.upload(path.name, path.read_bytes())
            second = self.upload('plain.pdf', self.pdf)
            job = self.process('merge', [first['id'], second['id']], {'password': password} if password else {})
            content = self.pdf_result(job)
            self.assertTrue(any('unencrypted working copy' in warning for warning in job['warnings']))
            with pymupdf.open(stream=content, filetype='pdf') as merged:
                self.assertIsNone(merged.metadata['encryption'])
                self.assertEqual([p.get_text() for p in merged], original_text + original_text)
            job = self.process('textlayout', [first['id']], {'password': password} if password else {})
            self.assertEqual(job['state'], 'completed', job)
            self.assertTrue(any('unencrypted working copies' in w for w in job['warnings']))
            self.assertNotIn('password', self.app.jobs[job['id']].options)

    def test_cancelled_creation_clears_values(self):
        path = self.root / 'cancel.pdf'; path.write_bytes(self.pdf)
        job = Job('forms', [{'id':'one','path':str(path),'name':path.name}], {'create_fields':self.specs()}, self.root / 'cancel')
        job.cancel_event.set(); job.run()
        self.assertEqual(job.state, 'cancelled')
        self.assertEqual(job.results, [])
        self.assertNotIn('create_fields', job.options)
