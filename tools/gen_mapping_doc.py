"""Write docs/mapping.md from the mapping spec in hl7bridge.v2fhir.spec.

Run it after changing the spec: python tools/gen_mapping_doc.py
A test fails while the file and the spec differ. With --check it writes
nothing and exits 1 when the file is out of date.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hl7bridge.v2fhir.spec import render_markdown

DOC = Path(__file__).resolve().parent.parent / "docs" / "mapping.md"


def main(argv: list[str]) -> int:
    text = render_markdown()
    if "--check" in argv:
        current = DOC.read_text(encoding="utf-8") if DOC.exists() else ""
        if current != text:
            print(f"{DOC} is out of date; run: python tools/gen_mapping_doc.py")
            return 1
        print(f"{DOC} matches the spec")
        return 0
    DOC.write_bytes(text.encode("utf-8"))
    print(f"wrote {DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
