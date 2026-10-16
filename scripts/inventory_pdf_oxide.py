"""Inventory the installed pinned public Python surface without parsing a document."""

import argparse
import importlib
import inspect
import json
from dataclasses import asdict
from pathlib import Path

from pdf_multitool.core import Limits
from pdf_multitool.isolation import install
from pdf_multitool.oxide import initialize_guard
from pdf_multitool.oxide_runtime import require_runtime
from pdf_multitool.worker import _limits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = asdict(Limits())
    _limits(config, "oxide")
    initialize_guard(install(), config)
    module = require_runtime({})
    entries = []
    public_exports = []
    names = getattr(module, "__all__", [name for name in dir(module) if not name.startswith("_")])
    exports = {name: getattr(module, name) for name in names}
    extension = importlib.import_module("pdf_oxide.pdf_oxide")
    exports.update(
        {
            "pdf_oxide.pdf_oxide." + name: getattr(extension, name)
            for name in dir(extension)
            if not name.startswith("_") and name not in names
        }
    )
    for name, value in sorted(exports.items()):
        origin = getattr(value, "__module__", "")
        if not origin.startswith("pdf_oxide"):
            continue
        public_exports.append(name)
        members = [(name, value)]
        if inspect.isclass(value):
            members.extend(
                (name + "." + key, inspect.getattr_static(value, key))
                for key in dir(value)
                if not key.startswith("_")
                or key in {"__init__", "__iter__", "__len__", "__getitem__"}
            )
        for public_name, member in members:
            try:
                signature = str(inspect.signature(member))
            except TypeError, ValueError:
                signature = None
            entries.append(
                {
                    "api": public_name,
                    "kind": "class"
                    if inspect.isclass(member)
                    else "callable"
                    if callable(member)
                    else "descriptor_or_constant",
                    "signature": signature,
                    "documentation": inspect.getdoc(member),
                    "implementation_status": "inventory_only_not_runtime_qualification",
                }
            )
    report = {
        "distribution": "pdf-oxide",
        "version": "0.3.78",
        "public_entries": len(entries),
        "public_exports": public_exports,
        "source": "https://github.com/yfedoseev/pdf_oxide/tree/v0.3.78",
        "scope": (
            "Actual installed public Python exports and additional native-extension exports; "
            "methods, value objects and async aliases included; Rust-only modules separately mapped"
        ),
        "qualification": "Presence is not proof of an implemented or correct workflow",
        "entries": entries,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "public_entries": len(entries),
                "exported_types_or_functions": len(public_exports),
            }
        )
    )


if __name__ == "__main__":
    main()
