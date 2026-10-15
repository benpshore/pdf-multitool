"""Frozen-scope synthetic regressions; no user PDFs or corpus changes."""

from copy import deepcopy

import pymupdf
import pytest

from pdf_multitool.analysis import (
    _order_baseline_pair,
    _ordered_blocks,
    export_html,
    export_markdown,
    export_text,
)


def pair(gap=6, words=("ALPHA", "BETA")):
    lines = []
    x = 40
    for text in words:
        box = (x, 50, x + len(text) * 6, 63)
        lines.append(
            {
                "bbox": box,
                "dir": (1, 0),
                "wmode": 0,
                "spans": [
                    {
                        "text": text,
                        "size": 10,
                        "font": "Courier",
                        "flags": 8,
                        "color": 0,
                        "origin": (x, 60),
                        "bbox": box,
                    }
                ],
            }
        )
        x = box[2] + gap
    return {"type": 0, "lines": list(reversed(lines))}


@pytest.mark.parametrize("gap", [2, 6, 7.5])
@pytest.mark.parametrize("words", [("ALPHA", "BETA"), ("one", "two")])
def test_clear_baseline_pair(gap, words):
    block = pair(gap, words)
    original = deepcopy(block)
    ordered = _order_baseline_pair(block)
    assert [line["spans"][0]["text"] for line in ordered["lines"]] == list(words)
    assert ordered["lines"] == list(reversed(block["lines"]))
    assert block == original
    assert _order_baseline_pair(ordered) is ordered


def negative_cases():
    for gap in (-1, 0, 1.99, 7.51, 250):
        yield f"gap-{gap}", pair(gap)
    for words in [
        ("אבגד", "הוזח"),
        ("ALPHA", "אבגד"),
        ("ALPHA", "BE\u202eTA"),
        ("A1", "B1"),
        ("12", "34"),
        ("A", "B"),
        ("WORD,", "NEXT"),
    ]:
        yield f"words-{words}", pair(words=words)
    for direction in [(0, 1), (-1, 0), (0, -1)]:
        b = pair()
        for line in b["lines"]:
            line["dir"] = direction
        yield f"direction-{direction}", b
    for key, value in [
        ("flags", 9),
        ("flags", 24),
        ("size", 8),
        ("color", 255),
        ("font", "Helvetica"),
        ("origin", (76, 59)),
    ]:
        b = pair()
        b["lines"][0]["spans"][0][key] = value
        yield f"span-{key}-{value}", b
    for key, value in [("wmode", 1), ("bbox", (76, 49, 100, 62))]:
        b = pair()
        b["lines"][0][key] = value
        yield f"line-{key}", b
    b = pair()
    b["lines"][0]["spans"].append(deepcopy(b["lines"][0]["spans"][0]))
    yield "multiple-spans", b
    b = pair()
    b["lines"].append(deepcopy(b["lines"][0]))
    yield "three-lines", b
    b = pair()
    b["type"] = 1
    yield "image", b
    for key in ("bbox", "origin", "size"):
        b = pair()
        del b["lines"][0]["spans"][0][key]
        yield f"missing-{key}", b
    for value in (float("nan"), float("inf")):
        b = pair()
        b["lines"][0]["spans"][0]["size"] = value
        yield f"nonfinite-{value}", b


@pytest.mark.parametrize(
    "name,block", list(negative_cases()), ids=lambda x: x if isinstance(x, str) else ""
)
def test_ambiguous_pairs_unchanged(name, block):
    assert _order_baseline_pair(block) is block, name


def make_document(reverse):
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    objects = [(40, "ALPHA"), (76, "BETA")]
    for x, text in reversed(objects) if reverse else objects:
        page.insert_text((x, 60), text, fontname="cour", fontsize=10)
    return doc


def test_real_engine_equivalent_pixels_and_exports(tmp_path):
    with make_document(False) as forward, make_document(True) as reverse:
        for dpi in (72, 144):
            assert forward[0].get_pixmap(dpi=dpi).samples == reverse[0].get_pixmap(dpi=dpi).samples
        raw = reverse[0].get_text("dict", sort=False)
        assert [line["spans"][0]["text"] for line in raw["blocks"][0]["lines"]] == ["BETA", "ALPHA"]
        for exporter, suffix in [
            (export_text, "txt"),
            (export_markdown, "md"),
            (export_html, "html"),
        ]:
            target = tmp_path / f"reverse.{suffix}"
            exporter(reverse, target, "synthetic.pdf")
            text = target.read_text()
            assert text.index("ALPHA") < text.index("BETA")
            if suffix == "txt":
                assert " ".join(text.split()) == "[Source page 1] ALPHA BETA"
        assert reverse[0].get_text("dict", sort=False) == raw


@pytest.mark.parametrize("angle", [90, 180, 270])
def test_rotated_page_not_repaired(angle):
    with make_document(True) as doc:
        page = doc[0]
        page.set_rotation(angle)
        assert _ordered_blocks(page) == page.get_text("dict", sort=False)["blocks"]


@pytest.mark.parametrize("offset", [0, 30])
def test_repeated_forms_preserve_occurrences(offset):
    with pymupdf.open() as source, pymupdf.open() as doc:
        source.new_page(width=150, height=100).insert_text((10, 30), "STAMP", fontname="cour")
        page = doc.new_page(width=600, height=800)
        page.show_pdf_page(pymupdf.Rect(40, 40, 190, 140), source, 0)
        page.show_pdf_page(pymupdf.Rect(40, 40 + offset, 190, 140 + offset), source, 0)
        raw = page.get_text("dict", sort=False)["blocks"]
        ordered = _ordered_blocks(page)
        assert ordered == raw
        assert (
            sum(
                span["text"].count("STAMP")
                for b in ordered
                for line in b["lines"]
                for span in line["spans"]
            )
            == 2
        )
