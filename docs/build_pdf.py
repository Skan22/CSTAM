"""Builds IPO-Diagrams.pdf: the system architecture, OpenStack high availability and gateway data
flow diagrams, one per A3 landscape page, as vectors, with no other text.

    python docs/build_pdf.py [output.pdf]

Needs headless Chromium. Re-run the three scripts in docs/diagrams first if a diagram changed.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

DOCS = Path(__file__).parent
PAGES = ["system-architecture", "openstack-ha", "gateway-data-flow"]
CSS = """@page { size: A3 landscape; margin: 0; } html, body { margin: 0; }
.p { width: 420mm; height: 297mm; display: flex; align-items: center; justify-content: center;
     break-after: page; } .p:last-child { break-after: auto; }
img { width: 404mm; height: 281mm; object-fit: contain; display: block; }"""


def build(out: Path) -> None:
    pages = "".join(f'<div class="p"><img src="{(DOCS / "diagrams" / (n + ".svg")).as_uri()}"></div>' for n in PAGES)
    html = f'<!doctype html><meta charset="utf-8"><title>Resilient IP Optimizer diagrams</title><style>{CSS}</style>{pages}'
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "d.html"
        src.write_text(html)
        subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={out}", "--virtual-time-budget=20000", f"file://{src}"],
                       capture_output=True, text=True, timeout=240, check=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else DOCS / "IPO-Diagrams.pdf").resolve())
