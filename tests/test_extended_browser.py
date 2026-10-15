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


@pytest.mark.skipif(
    not importlib.util.find_spec("playwright") or not shutil.which("chromium"),
    reason="Requires playwright and system chromium",
)
def test_keyboard_native_zoom_and_mobile(tmp_path, monkeypatch):
    from scripts.qualify_gui_usability import main

    output = tmp_path / "usability"
    monkeypatch.setattr(sys, "argv", ["qualify_gui_usability", "--output", str(output)])
    main()
    report = json.loads((output / "report.json").read_text())
    assert report["passed"]
    assert report["native_browser_zoom"] == 2
    assert report["jobs_created"] == 1
    assert set(report["layouts"]) == {
        "desktop",
        "native_zoom_200_percent",
        "mobile_390",
        "mobile_390_dark",
        "mobile_320",
        "mobile_320_dark",
    }
