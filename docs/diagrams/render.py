"""Renders every *.mmd in this directory to .svg and .png, with headless Chromium and Mermaid.

    python docs/diagrams/render.py [name ...]

Needs `chromium` on the PATH and network access to cdn.jsdelivr.net (Mermaid is loaded from
there). The .mmd files are the source of truth for the sequence and state diagrams; the three
architecture figures are hand-laid-out SVG (gateway_data_flow.py, system_architecture.py,
openstack_ha.py), because automatic layout tangled them. The Markdown documents embed the .png
files and link to the .svg ones.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"
THEME = {"theme": "base", "themeVariables": {
    "fontFamily": "Inter, Helvetica, Arial, sans-serif", "fontSize": "15px",
    "primaryColor": "#f1eaff", "primaryBorderColor": "#7a4fd6", "lineColor": "#555",
    "primaryTextColor": "#222"}}


def page(source: str) -> str:
    import json
    return (f'<!doctype html><meta charset="utf-8"><body style="margin:0;background:#fff">'
            f'<pre class="mermaid">{source.replace("<", "&lt;").replace("&lt;br/>", "<br/>")}</pre>'
            f'<script type="module">import mermaid from "{MERMAID}";'
            f'mermaid.initialize({{startOnLoad:false,securityLevel:"loose",{json.dumps(THEME)[1:-1]}}});'
            f'await mermaid.run();</script></body>')


def chromium(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", *args],
                          capture_output=True, text=True, timeout=180)


def render(mmd: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        html = Path(tmp) / "d.html"
        html.write_text(page(mmd.read_text()))
        dom = chromium("--virtual-time-budget=25000", "--dump-dom", f"file://{html}").stdout
        m = re.search(r"<svg.*?</svg>", dom, re.S)
        if not m or "Syntax error" in m.group(0):
            raise SystemExit(f"{mmd.name}: Mermaid could not render it\n{m.group(0)[:400] if m else dom[-400:]}")
        svg = m.group(0)
        vb = re.search(r'viewBox="([\d.\- ]+)"', svg)
        assert vb
        w, h = (float(x) for x in vb.group(1).split()[2:])
        svg = re.sub(r'(<svg[^>]*?)\swidth="100%"', rf'\1 width="{w:.0f}" height="{h:.0f}"', svg, 1)
        svg = re.sub(r'style="max-width:[^"]*;?"', "", svg, 1)
        svg_path = mmd.with_suffix(".svg")
        svg_path.write_text(svg)
        shot = Path(tmp) / "s.html"
        shot.write_text(f'<body style="margin:0;background:#fff">{svg}</body>')
        chromium(f"--window-size={int(w) + 4},{int(h) + 4}", "--force-device-scale-factor=2",
                 f"--screenshot={mmd.with_suffix('.png')}", f"file://{shot}")
        print(f"{mmd.stem}: {int(w)}x{int(h)}")


if __name__ == "__main__":
    wanted = set(sys.argv[1:])
    for f in sorted(HERE.glob("*.mmd")):
        if not wanted or f.stem in wanted:
            render(f)
