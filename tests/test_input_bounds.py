"""Synthetic boundary regressions; worker publication tests use explicit stubs."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pdf_multitool.cli import main
from pdf_multitool.core import Job, JobError, read_local_json, strict_json_loads
from pdf_multitool.server import make_handler


class StrictNumberTests(unittest.TestCase):
    def test_exponent_overflow_rejected_at_every_depth(self):
        for token in ("1e999", "-1e999", "1.8e308", "-1.8e308"):
            for source in (token, "[" + token + "]", '{"a":[{"b":' + token + "}]}"):
                with self.subTest(source=source), self.assertRaisesRegex(JobError, "non-finite"):
                    strict_json_loads(source)

    def test_finite_numbers_and_arbitrary_precision_integers_preserved(self):
        source = '{"large":1.7976931348623157e308,"small":1e-999,"integer":' + "9" * 400 + "}"
        self.assertEqual(strict_json_loads(source), json.loads(source))

    def test_local_json_rejects_exponent_overflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fields.json"
            path.write_text('{"size":1e999}', encoding="utf-8")
            with self.assertRaisesRegex(JobError, "non-finite"):
                read_local_json(path)

    def test_api_dispatch_rejects_overflow_without_creating_job(self):
        # Exercise the real POST dispatcher using in-memory I/O, no listener.
        for route in ("/api/jobs", "/api/bundles"):
            with self.subTest(route=route):
                app = Mock()
                handler_type = make_handler(app)
                handler = object.__new__(handler_type)
                handler.path = route
                handler.allowed = lambda: True
                source = b'{"options":{"dpi":1e999}}'
                handler.headers = {"Content-Length": str(len(source))}
                handler.rfile = io.BytesIO(source)
                handler.send_json = Mock()
                handler.do_POST()
                response, status = handler.send_json.call_args.args
                self.assertEqual(status, 400)
                self.assertIn("non-finite", response["error"])
                self.assertTrue(handler.close_connection)
                app.executor.submit.assert_not_called()

    def test_cli_rejects_overflow_before_job_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "synthetic.pdf"
            source.write_bytes(b"not parsed: invalid config must fail first")
            config = root / "fields.json"
            config.write_text('{"Value":-1e999}', encoding="utf-8")
            stderr = io.StringIO()
            with (
                patch(
                    "pdf_multitool.cli.Job",
                    side_effect=AssertionError("invalid config reached Job"),
                ) as job,
                contextlib.redirect_stderr(stderr),
            ):
                code = main(
                    ["forms", str(source), "--fields-json", str(config), "-o", str(root / "out")]
                )
            self.assertEqual(code, 1)
            self.assertIn("non-finite", json.loads(stderr.getvalue())["error"])
            job.assert_not_called()


class AtomicPublicationTests(unittest.TestCase):
    def run_job(self, count, fail_part=1, error="encoding"):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        files = [{"id": str(i), "name": f"{i}.pdf", "path": f"{i}.pdf"} for i in range(count)]
        job = Job("text", files, {}, Path(self.temp.name))

        def worker(payload, workdir, cancel, limits):
            workdir.mkdir()
            (workdir / "first.pdf").write_bytes(b"%PDF synthetic publication stub")
            outputs = [{"file": "first.pdf", "name": "CON.pdf"}]
            if workdir.name == f"part-{fail_part:04d}":
                if error == "encoding":
                    (workdir / "second.txt").write_bytes(b"\xff")
                    outputs.append({"file": "second.txt", "name": "second.txt"})
                else:
                    outputs.append({"file": "../missing.pdf", "name": "missing.pdf"})
            return {"ok": True, "outputs": outputs, "warnings": ["worker warning"]}

        with patch("pdf_multitool.core.run_worker", side_effect=worker):
            job.run()
        return job

    def test_first_part_failure_publishes_nothing(self):
        for error in ("encoding", "location"):
            with self.subTest(error=error):
                job = self.run_job(1, error=error)
                self.assertEqual(job.state, "failed")
                self.assertEqual(job.results, [])
                self.assertEqual(job.warnings, [])
                self.assertEqual(len(job.errors), 1)
                self.assertEqual(list(job.directory.iterdir()), [])

    def test_failed_part_preserves_only_prior_completed_part(self):
        job = self.run_job(2, fail_part=2)
        self.assertEqual(job.state, "partial")
        self.assertEqual(len(job.results), 1)
        self.assertTrue(Path(job.results[0]["path"]).is_file())
        self.assertEqual(len(job.errors), 1)
        self.assertFalse((job.directory / "part-0002").exists())
        self.assertIn("worker warning", job.warnings)

    def test_later_part_can_succeed_after_failed_part(self):
        job = self.run_job(2)
        self.assertEqual(job.state, "partial")
        self.assertEqual(len(job.results), 1)
        self.assertIn("part-0002", job.results[0]["path"])
        self.assertTrue(Path(job.results[0]["path"]).is_file())
        self.assertEqual(len(job.errors), 1)


if __name__ == "__main__":
    unittest.main()
