"""Opt-in full-stack browser exercises for every additional adapter."""

import importlib.util
import json
import shutil
import sys

import pytest


@pytest.mark.skipif(
    not importlib.util.find_spec("playwright") or not shutil.which("chromium"),
    reason="Requires playwright and system chromium",
)
def test_additional_browser_workflows(tmp_path, monkeypatch):
    from scripts.qualify_engine_workflows import main

    output = tmp_path / "inspection"
    monkeypatch.setattr(sys, "argv", ["qualify_engine_workflows", "--output", str(output)])
    main()
    report = json.loads((output / "report.json").read_text())
    assert len(report["scenarios"]) == 12
    assert all(s["snapshot"]["state"] == "completed" for s in report["scenarios"])
    assert not report["browser_errors"]
    assert report["page_inventory"]
