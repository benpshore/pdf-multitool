"""Synthetic qualification probe for upstream PyMuPDF issue 5152.

Run in the project environment. Exit nonzero if rectangle redaction leaves
readable text. This is a qualification probe, not an expected-failure waiver.
"""

import json
import tempfile
from pathlib import Path

import pymupdf

from pdf_multitool.core import Job


def main():
    results = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "synthetic.pdf"
        target_text = "PRIVATE_5152"
        with pymupdf.open() as document:
            page = document.new_page(width=320, height=240)
            page.insert_text((20, 30), "font resource", fontname="helv")
            font = page.get_fonts()[0][4]
            stream = f"BT /{font} 12 Tf 25 190 Td 18 TL (KEEP) Tj 0 -35 Td ({target_text}) ' ET"
            document.update_stream(page.get_contents()[0], stream.encode())
            rectangle = list(page.search_for(target_text)[0])
            document.save(source)
        for mode, options in (
            ("text", {"redact_text": target_text}),
            ("rectangle", {"redact_boxes": [{"page": 1, "rect": rectangle}]}),
        ):
            job = Job(
                "redact",
                [{"id": mode, "name": source.name, "path": str(source)}],
                options,
                root / "jobs",
            )
            job.run()
            leaked = False
            for result in job.results:
                if Path(result["path"]).suffix == ".pdf":
                    with pymupdf.open(result["path"]) as output:
                        leaked |= any(target_text in page.get_text() for page in output)
            results.append({"mode": mode, "state": job.state, "leaked": leaked})
        print(json.dumps({"pymupdf": pymupdf.VersionBind, "results": results}))
    return int(any(result["leaked"] for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
