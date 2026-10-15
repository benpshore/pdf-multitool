"""Frozen real-PDF exclusions and safe nonfinite-coordinate unit probes."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pymupdf
import pytest

from pdf_multitool.analysis import _order_baseline_pair, _ordered_blocks

ROOT = Path(__file__).resolve().parents[1] / "experiments/ltr_boundaries_v1"
CONTRACTS = json.loads((ROOT / "contracts.json").read_text())
FROZEN = json.loads((ROOT / "FROZEN.json").read_text())


@pytest.mark.parametrize("name,case", CONTRACTS["cases"].items())
def test_real_pdf_local_contract(name, case):
    path = ROOT / "fixtures" / f"{name}.pdf"
    original = path.read_bytes()
    assert hashlib.sha256(original).hexdigest() == FROZEN["pdf_sha256"][name]
    with pymupdf.open(path) as doc:
        assert len(doc) == 1
        page = doc[0]
        raw = page.get_text("dict", sort=False)
        snapshot = deepcopy(raw)
        local = [_order_baseline_pair(block) for block in raw["blocks"]]
        if case["policy"] == "retain":
            assert all(a is b for a, b in zip(local, raw["blocks"], strict=True))
        else:
            left, right = case.get("words", ["ALPHA", "BETA"])
            assert len(raw["blocks"]) == 1
            before = raw["blocks"][0]["lines"]
            after = local[0]["lines"]
            assert [line["spans"][0]["text"] for line in before] == [right, left]
            assert [line["spans"][0]["text"] for line in after] == [left, right]
        _ordered_blocks(page)
        assert raw == snapshot
        assert page.get_text("dict", sort=False) == snapshot
    assert path.read_bytes() == original


def coordinate_targets():
    for component in range(4):
        yield "block", 0, "bbox", component
    for line in range(2):
        for component in range(4):
            yield "line", line, "bbox", component
            yield "span", line, "bbox", component
        for component in range(2):
            yield "span", line, "origin", component


@pytest.mark.parametrize("target,index,key,component", list(coordinate_targets()))
@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "posinf", "neginf"]
)
def test_nonfinite_geometry_retains_original(target, index, key, component, value):
    with pymupdf.open(ROOT / "fixtures/positive_reverse.pdf") as doc:
        block = doc[0].get_text("dict", sort=False)["blocks"][0]
    if target == "block":
        item = block
    elif target == "line":
        item = block["lines"][index]
    else:
        item = block["lines"][index]["spans"][0]
    coordinates = list(item[key])
    coordinates[component] = value
    item[key] = tuple(coordinates)
    before = list(block["lines"])
    assert _order_baseline_pair(block) is block
    assert all(a is b for a, b in zip(before, block["lines"], strict=True))
