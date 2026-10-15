"""Bounded synthetic probes: no network traffic and at most one child process."""

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace
from unittest.mock import patch

import pytest

from pdf_multitool.core import Job, JobError, Limits, run_worker
from tests.fixtures import document


@pytest.mark.skipif(sys.platform != "linux", reason="Linux libseccomp qualification")
def test_filter_blocks_native_socket_and_survives_helper_exec():
    script = """
import ctypes, errno, json, os, subprocess, sys
from pdf_multitool.isolation import install
status = install()
libc = ctypes.CDLL(None, use_errno=True)
assert libc.socket(2, 1, 0) == -1
assert ctypes.get_errno() == errno.EPERM
try:
    os.setsid()
except PermissionError:
    pass
else:
    raise AssertionError('escaped process group')
probe = 'import socket; socket.socket()'
child = subprocess.run([sys.executable, '-c', probe], capture_output=True, timeout=5)
assert child.returncode != 0 and b'PermissionError' in child.stderr
print(json.dumps(status))
"""
    process = subprocess.run(  # noqa: S603 -- fixed, bounded probe; no external request
        [sys.executable, "-c", script], capture_output=True, timeout=10, check=False
    )
    assert process.returncode == 0, process.stderr
    status = json.loads(process.stdout)
    assert status["native_network_filter"] is True
    assert status["process_group_confinement"] is True
    assert status["filesystem_isolation"] is False


@pytest.mark.skipif(sys.platform != "linux" or os.getuid() == 0, reason="Nonroot Linux limits")
def test_process_limit_denies_single_fork_and_memory_allocation_is_bounded():
    # Zero child allowance is safer than a fork loop, and directly proves kernel enforcement.
    for probe in (
        "resource.setrlimit(resource.RLIMIT_NPROC,(0,0)); "
        "\ntry: subprocess.run(['/usr/bin/true'], timeout=2)\n"
        "except BlockingIOError: print('bounded')\nelse: raise AssertionError('fork allowed')",
        "resource.setrlimit(resource.RLIMIT_AS,(64*1024**2,64*1024**2)); "
        "\ntry: bytearray(256*1024**2)\n"
        "except MemoryError: print('bounded')\nelse: raise AssertionError('allocation allowed')",
    ):
        process = subprocess.run(  # noqa: S603 -- bounded allocation/single-fork test
            [sys.executable, "-c", "import resource,subprocess; " + probe],
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert process.returncode == 0, process.stderr
        assert process.stdout.strip() == b"bounded"


def test_deadline_covers_worker_stalled_before_reading_stdin(tmp_path):
    spawn = subprocess.Popen
    children = []

    def stalled(args, **kwargs):
        child = spawn([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    started = time.monotonic()
    with (
        patch("pdf_multitool.core.subprocess.Popen", stalled),
        pytest.raises(JobError, match="exceeded"),
    ):
        run_worker(
            {
                "operation": "rotate",
                "inputs": [],
                "names": [],
                "options": {"padding": "x" * 190000},
            },
            tmp_path / "worker",
            threading.Event(),
            replace(Limits(), timeout_seconds=1),
        )
    assert time.monotonic() - started < 5
    assert len(children) == 1 and children[0].poll() is not None


def test_worker_applies_declared_resource_bounds(tmp_path):
    limits = asdict(Limits())
    script = """
import json, resource, sys
from pdf_multitool.worker import _limits
config=json.loads(sys.argv[1]); _limits(config, 'rotate')
print(json.dumps({name: resource.getrlimit(getattr(resource,name)) for name in
('RLIMIT_NPROC','RLIMIT_NOFILE','RLIMIT_FSIZE','RLIMIT_AS','RLIMIT_CPU')}))
"""
    process = subprocess.run(  # noqa: S603 -- fixed disposable resource probe
        [sys.executable, "-c", script, json.dumps(limits)],
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["RLIMIT_NPROC"][0] == result["RLIMIT_NPROC"][1]
    assert 64 <= result["RLIMIT_NPROC"][0] <= 4096 + 64
    assert result["RLIMIT_NOFILE"] == [512, 512]
    assert result["RLIMIT_AS"] == [limits["memory_mb"] * 1024**2] * 2


def test_symlink_result_is_never_published(tmp_path):
    source = document(tmp_path / "original.pdf")
    before = source.read_bytes()
    job = Job(
        "rotate", [{"id": "f", "path": str(source), "name": source.name}], {}, tmp_path / "jobs"
    )

    def hostile(payload, workdir, cancel, limits):
        workdir.mkdir()
        (workdir / "real.pdf").write_bytes(before)
        (workdir / "link.pdf").symlink_to(workdir / "real.pdf")
        return {"ok": True, "outputs": [{"file": "link.pdf", "name": "leak.pdf"}]}

    with patch("pdf_multitool.core.run_worker", hostile):
        job.run()
    assert job.state == "failed"
    assert not job.results
    assert list(job.directory.iterdir()) == []
    assert source.read_bytes() == before


def test_cancel_new_engine_reaps_child_and_discards_unpublished_pdf(tmp_path):
    source = document(tmp_path / "original.pdf")
    job = Job(
        "paginate", [{"id": "f", "path": str(source), "name": source.name}], {}, tmp_path / "jobs"
    )
    spawn = subprocess.Popen
    children = []
    code = """
import time
from pdf_multitool import worker
original = worker.process
def held(request):
    result = original(request)
    time.sleep(30)
    return result
worker.process = held
worker.main()
"""

    def held(args, **kwargs):
        child = spawn([sys.executable, "-c", code], **kwargs)
        children.append(child)
        return child

    with patch("pdf_multitool.core.subprocess.Popen", held):
        thread = threading.Thread(target=job.run)
        thread.start()
        deadline = time.monotonic() + 10
        try:
            while not list(job.directory.rglob("paginated.pdf")) and time.monotonic() < deadline:
                thread.join(0.02)
            assert list(job.directory.rglob("paginated.pdf")), job.snapshot()
            assert job.results == []
        finally:
            job.cancel_event.set()
            thread.join(8)
    assert not thread.is_alive()
    assert job.state == "cancelled"
    assert not job.results
    assert all(child.poll() is not None for child in children)
    assert list(job.directory.iterdir()) == []
