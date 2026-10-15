"""Independent acceptance gates; correction mode must fail while table oracles fail."""

import argparse
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORIGINAL = ROOT.parent / "representation_v1"


def normalized(text):
    return " ".join(text.split())


class Content(HTMLParser):
    """Read content in DOM order, including nested styling; no token sorting."""

    def __init__(self, markdown=False):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.markdown = markdown

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        active = any(self.stack) or (
            tag == "code" if self.markdown else "text-line" in attrs.get("class", "").split()
        )
        self.stack.append(active)
        if active:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if not self.stack:
            raise ValueError("Unexpected closing markup")
        if self.stack.pop():
            self.parts.append(" ")

    def handle_data(self, data):
        if any(self.stack):
            self.parts.append(data)
        elif self.markdown and data.strip():
            raise ValueError("Unsupported frozen Markdown body syntax; do not silently discard")


def export_body(case, kind):
    text = (case / f"export.{kind}").read_text()
    if kind == "txt":
        prefix = "[Source page 1]\n\n"
        if not text.startswith(prefix):
            raise ValueError("Unexpected text preamble")
        return normalized(text[len(prefix) :])
    if kind == "md":
        start = '<a id="page-1"></a>\n\n## Source page 1\n\n'
        end = "## Export observations and limitations\n\n"
        if text.count(start) != 1 or text.count(end) != 1:
            raise ValueError("Unexpected Markdown boundaries")
        text = text.split(start)[1].split(end)[0]
    parser = Content(markdown=kind == "md")
    parser.feed(text)
    parser.close()
    return normalized("".join(parser.parts))


def file_hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--repeat", type=Path, required=True)
    parser.add_argument("--mode", choices=["limitation", "correction"], required=True)
    args = parser.parse_args()
    contracts = json.loads((ROOT / "contracts.json").read_text())
    frozen = json.loads((ROOT / "PRECAPTURE.json").read_text())
    protocol_intact = all(
        hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
        for name, digest in frozen["files"].items()
    )
    original_intact = file_hashes(ORIGINAL) == json.loads(
        (ROOT / "original_tree_sha256.json").read_text()
    )
    repeated = file_hashes(args.run) == file_hashes(args.repeat)
    summary = json.loads((args.run / "summary.json").read_text())
    expected = json.loads((ORIGINAL / "expectations.json").read_text())["fixtures"]
    if set(summary["cases"]) != set(expected):
        raise ValueError("Incomplete fixture inventory")
    cases = {}
    for name, oracle in expected.items():
        row = summary["cases"][name]
        exports = {kind: export_body(args.run / name, kind) for kind in ["txt", "md", "html"]}
        stages = {
            key: row[key]
            for key in ["parser_block_text", "split_block_text", "application_ordered_text"]
        }
        cases[name] = {
            "expected": oracle,
            "stages": stages,
            "exports": exports,
            "matches": {kind: text == oracle for kind, text in exports.items()},
        }
        if name.startswith("table_"):
            cases[name]["row_pairs"] = {
                kind: {f"A{i} B{i}": f"A{i} B{i}" in text for i in range(1, 4)}
                for kind, text in exports.items()
            }
        if name.startswith("prose_"):
            cases[name]["column_chains"] = {
                kind: [
                    token for token in text.split() if token in ["L1", "L2", "L3", "R1", "R2", "R3"]
                ]
                for kind, text in exports.items()
            }
    controls = {
        control["name"]: (normalized(control["observed"]) == normalized(control["expected"]))
        == control["accept"]
        for control in contracts["scorer_controls"]
    }
    positive_pass = all(all(cases[n]["matches"].values()) for n in contracts["positive_cases"])
    historical = json.loads((ORIGINAL / "observations/summary.json").read_text())
    negative_visible = all(
        all(
            text == historical["cases"][n]["structured_text_body"]
            for text in cases[n]["exports"].values()
        )
        for n in contracts["known_negative_cases"]
    )
    pixel_groups = all(all(g["equal_render_samples"].values()) for g in summary["groups"].values())
    gates = {
        "original_tree_intact": original_intact,
        "precapture_protocol_intact": protocol_intact,
        "two_runs_byte_identical": repeated,
        "positive_controls": positive_pass,
        "scorer_controls": all(controls.values()),
        "render_invariance_only": pixel_groups,
        "pinned_main": summary["production_base"] == frozen["main"],
    }
    if args.mode == "correction":
        gates["all_frozen_export_oracles"] = all(all(c["matches"].values()) for c in cases.values())
    else:
        gates["known_table_failures_explicit"] = negative_visible
    result = {
        "mode": args.mode,
        "accepted": all(gates.values()),
        "gates": gates,
        "scorer_controls": controls,
        "cases": cases,
        "scope": "Designed frozen cases only; no corpus accuracy or held-out heuristic claim.",
    }
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["accepted"] else 1)


if __name__ == "__main__":
    main()
