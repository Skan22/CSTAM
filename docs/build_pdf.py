"""Builds IPO-Architecture.pdf: the system architecture, OpenStack HA and gateway data flow
documents with their diagrams, as one document for reading or printing.

    python docs/build_pdf.py [output.pdf]

Uses markdown-it-py (any Python with it) and headless Chromium. Diagrams stay vectors (SVG) and
the big ones get a landscape page of their own. Relative links are dropped to plain text.
"""

import datetime
import html
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt

DOCS = Path(__file__).parent
PARTS = [
    ("Part 1", "System architecture", DOCS / "architecture/system-architecture.md"),
    ("Part 2", "OpenStack high availability", DOCS / "architecture/openstack-ha.md"),
    ("Part 3", "Gateway data flow", DOCS / "architecture/gateway-data-flow.md"),
]
CSS = """
@page { size: A4 portrait; margin: 18mm 16mm 18mm 16mm;
  @bottom-center { content: counter(page) " / " counter(pages); font: 8pt 'Noto Sans', sans-serif; color: #777; }
  @top-right { content: "Resilient IP Optimizer"; font: 8pt 'Noto Sans', sans-serif; color: #999; } }
@page wide { size: A4 landscape; margin: 10mm 10mm 12mm 10mm;
  @bottom-center { content: counter(page) " / " counter(pages); font: 8pt 'Noto Sans', sans-serif; color: #777; } }
@page cover { margin: 0; @bottom-center { content: none; } @top-right { content: none; } }
html { font: 10pt/1.5 'Noto Sans', 'DejaVu Sans', Helvetica, Arial, sans-serif; color: #1d1d2b; }
body { margin: 0; }
h1 { font-size: 22pt; line-height: 1.2; margin: 0 0 6mm; color: #2a1457; break-before: page; }
h1 .part { display: block; font-size: 10pt; letter-spacing: .08em; text-transform: uppercase; color: #c98a00; margin-bottom: 2mm; }
h2 { font-size: 14pt; margin: 8mm 0 3mm; color: #2a1457; border-bottom: 1px solid #d9d2ec; padding-bottom: 1mm; break-after: avoid; }
h3 { font-size: 11.5pt; margin: 6mm 0 2mm; color: #3b2a70; break-after: avoid; }
p, li { orphans: 3; widows: 3; }
p { margin: 0 0 3mm; } ul, ol { margin: 0 0 3mm; padding-left: 6mm; } li { margin-bottom: 1mm; }
code { font: 8.6pt 'DejaVu Sans Mono', monospace; background: #f1eefa; padding: 0 1.2mm; border-radius: 1mm; }
pre { background: #f6f4fb; border: 1px solid #e2dcf2; border-radius: 2mm; padding: 3mm; margin: 0 0 4mm;
  white-space: pre-wrap; word-break: break-all; break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 7.6pt; line-height: 1.35; }
table { border-collapse: collapse; width: 100%; margin: 0 0 4mm; font-size: 8.6pt; }
thead { display: table-header-group; } tr { break-inside: avoid; }
th { text-align: left; background: #efe9fb; color: #2a1457; }
th, td { border: 1px solid #dcd5ee; padding: 1.3mm 2mm; vertical-align: top; }
em { color: #555; }
a { color: inherit; text-decoration: none; }
figure { margin: 0 0 4mm; text-align: center; }
figure img { max-width: 100%; }
figcaption { font-size: 8pt; color: #777; margin-top: 1mm; }
section.wide { page: wide; break-before: page; break-after: page; }
section.wide figure { margin: 0; } section.wide img { display: block; width: 277mm; height: 182mm; object-fit: contain; }
section.wide figcaption { display: none; }
figure.tall img { max-height: 215mm; width: auto; }
.cover { page: cover; break-after: page; height: 297mm; background: #2e0a40; color: #fff; padding: 60mm 24mm 0; box-sizing: border-box; }
.cover .brand { display: inline-block; background: #ffc914; color: #120a1a; font-weight: 700; padding: 1.5mm 4mm; border-radius: 1.5mm; letter-spacing: .04em; }
.cover h1 { color: #fff; font-size: 34pt; margin: 14mm 0 4mm; break-before: auto; }
.cover .sub { font-size: 15pt; color: #e6dcff; line-height: 1.4; max-width: 150mm; }
.cover .meta { margin-top: 28mm; font-size: 10pt; color: #cbbbee; line-height: 1.8; }
.toc { break-after: page; } .toc h1 { break-before: auto; }
.toc ol { list-style: none; padding: 0; } .toc li { margin: 0 0 2.5mm; font-size: 11pt; }
.toc li small { display: block; color: #666; font-size: 8.6pt; margin: .5mm 0 0 8mm; }
.toc .n { display: inline-block; width: 8mm; color: #c98a00; font-weight: 700; }
"""


def svg_size(svg: Path) -> tuple[float, float]:
    m = re.search(r'viewBox="[\d.\-]+ [\d.\-]+ ([\d.]+) ([\d.]+)"', svg.read_text()[:3000])
    assert m, svg
    return float(m.group(1)), float(m.group(2))


def render_part(path: Path, label: str, title: str) -> tuple[str, list[str]]:
    md = MarkdownIt("commonmark").enable("table")
    text = path.read_text()
    text = re.sub(r"^# .*\n", "", text, count=1)  # the part title is added below
    text = re.sub(r"(?<!!)\[([^\]]+)\]\((?!https?://)[^)]*\)", r"\1", text)  # relative links -> text; images stay
    body = md.render(text)

    def figure(m: re.Match[str]) -> str:
        src, alt = m.group(1), html.unescape(m.group(2))
        name = Path(src).stem
        svg = DOCS / "diagrams" / f"{name}.svg"
        w, h = svg_size(svg)
        img = f'<img src="{svg.as_uri()}" alt="{html.escape(alt)}">'
        cap = f"<figcaption>{html.escape(alt)}</figcaption>"
        if w >= 1100:
            return f'<section class="wide"><figure>{img}{cap}</figure></section>'
        return f'<figure class="{"tall" if h > w else ""}">{img}{cap}</figure>'

    body = re.sub(r'<p><img src="([^"]+)" alt="([^"]*)"\s*/?></p>', figure, body)
    heads = re.findall(r"<h2>(.*?)</h2>", body)
    return f'<h1><span class="part">{label}</span>{html.escape(title)}</h1>\n{body}', heads


def build(out: Path) -> None:
    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=DOCS, capture_output=True,
                         text=True).stdout.strip()
    sections, toc = [], []
    for i, (label, title, path) in enumerate(PARTS, 1):
        html_part, heads = render_part(path, label, title)
        sections.append(html_part)
        sub = " · ".join(re.sub(r"<[^>]+>", "", h) for h in heads[:8])
        toc.append(f'<li><span class="n">{i}</span><b>{html.escape(title)}</b><small>{html.escape(sub)}</small></li>')
    cover = f"""<div class="cover"><span class="brand">IEEE CSTAM 3.0 × FELCLOUD</span>
<h1>Resilient IP Optimizer</h1><div class="sub">System architecture, OpenStack high availability
and the gateway data flow</div>
<div class="meta">Source: docs/architecture and docs/diagrams<br>Repository revision {html.escape(rev)}<br>
{datetime.date.today():%d %B %Y}</div></div>"""
    contents = f'<div class="toc"><h1>Contents</h1><ol>{"".join(toc)}</ol></div>'
    page = (f'<!doctype html><html><head><meta charset="utf-8"><title>Resilient IP Optimizer: architecture</title>'
            f"<style>{CSS}</style></head><body>{cover}{contents}{''.join(sections)}</body></html>")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "doc.html"
        src.write_text(page)
        subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={out}", "--virtual-time-budget=20000", f"file://{src}"],
                       capture_output=True, text=True, timeout=240, check=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else DOCS / "IPO-Architecture.pdf").resolve())
