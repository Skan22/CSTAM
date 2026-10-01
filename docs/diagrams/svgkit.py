"""Tiny SVG toolkit for the hand-laid-out diagrams (gateway_data_flow.py, system_architecture.py,
openstack_ha.py). Boxes, arrows with badges, lanes, and a Chromium PNG export; no dependencies."""

import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).parent
W, H = 1720, 1200
COL = {  # fill, stroke, text
    "req": ("#e8f1ff", "#2f6fdd", "#10294d"), "cfg": ("#f1eaff", "#7a4fd6", "#2a1457"),
    "sig": ("#fff4d6", "#c98a00", "#4d3300"), "tel": ("#e0f7f4", "#1a9c8b", "#08413a"),
    "store": ("#f4f4f4", "#777", "#222"), "ext": ("#ffffff", "#555", "#222"),
    "bad": ("#fdeaea", "#c24141", "#4d1010"), "ok": ("#e8f8ec", "#2f9e55", "#0f3a1d"),
}
out: list[str] = []


def box(x: int, y: int, w: int, h: int, kind: str, title: str, *lines: str, dashed: bool = False,
        cyl: bool = False) -> tuple[int, int, int, int]:
    fill, stroke, text = COL[kind]
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    if cyl:
        out.append(f'<path d="M{x},{y + 10} a{w / 2},10 0 0 1 {w},0 v{h - 20} a{w / 2},10 0 0 1 -{w},0 z" '
                   f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>'
                   f'<path d="M{x},{y + 10} a{w / 2},10 0 0 0 {w},0" fill="none" stroke="{stroke}"/>')
    else:
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" '
                   f'stroke="{stroke}" stroke-width="1.6"{dash}/>')
    n = 1 + len(lines)
    top = y + h / 2 - (n - 1) * 10 + (5 if cyl else 0)
    out.append(f'<text x="{x + w / 2}" y="{top}" text-anchor="middle" font-weight="700" fill="{text}">'
               f'{escape(title)}</text>')
    for i, ln in enumerate(lines):
        out.append(f'<text x="{x + w / 2}" y="{top + 20 * (i + 1)}" text-anchor="middle" font-size="13" '
                   f'fill="{text}">{escape(ln)}</text>')
    return x, y, w, h


def arrow(pts: list[tuple[float, float]], label: str = "", kind: str = "req", dashed: bool = False,
          num: str = "", seg: int = 0, dy: float = -22) -> None:
    """A polyline arrow; the badge sits on the middle of segment `seg`, the label above it."""
    stroke = COL[kind][1]
    d = "M" + " L".join(f"{x},{y}" for x, y in pts)
    dash = ' stroke-dasharray="7 5"' if dashed else ""
    out.append(f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="2.2"{dash} '
               f'marker-end="url(#a-{kind})"/>')
    (x1, y1), (x2, y2) = pts[seg], pts[seg + 1]
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    if num:
        out.append(f'<circle cx="{mx}" cy="{my}" r="11" fill="{stroke}"/><text x="{mx}" y="{my + 4.5}" '
                   f'text-anchor="middle" font-size="12" font-weight="700" fill="#fff">{escape(num)}</text>')
    if label:
        out.append(f'<text x="{mx}" y="{my + dy}" text-anchor="middle" font-size="12.5" '
                   f'fill="{COL[kind][2]}">{escape(label)}</text>')


def lane(y: int, h: int, title: str, sub: str, kind: str) -> None:
    fill, stroke, text = COL[kind]
    out.append(f'<rect x="20" y="{y}" width="{W - 40}" height="{h}" rx="14" fill="{fill}" '
               f'fill-opacity="0.35" stroke="{stroke}" stroke-width="1.4"/>')
    out.append(f'<text x="40" y="{y + 28}" font-size="18" font-weight="700" fill="{text}">'
               f'{escape(title)}</text><text x="40" y="{y + 48}" font-size="13" fill="{text}">'
               f'{escape(sub)}</text>')


def spread(widths: list[int], left: int = 50, right: int | None = None) -> list[int]:
    """x positions that space boxes of these widths evenly between the lane's margins."""
    right = W - 50 if right is None else right
    gap = (right - left - sum(widths)) / (len(widths) - 1)
    xs, x = [], float(left)
    for w in widths:
        xs.append(round(x))
        x += w + gap
    return xs


def chain(y: int, nodes: list[tuple[int, str, str, tuple[str, ...]]], arrows: list[tuple[str, str]],
          kind: str, **kw: object) -> list[tuple[int, int, int, int]]:
    xs = spread([n[0] for n in nodes])
    boxes = [box(x, y, w, 84, k, t, *ls, **kw) for x, (w, k, t, ls) in zip(xs, nodes, strict=True)]  # type: ignore[arg-type]
    for i, (lab, num) in enumerate(arrows):
        (x1, y1, w1, h1), (x2, y2, _, _) = boxes[i], boxes[i + 1]
        arrow([(x1 + w1 + 2, y1 + h1 / 2), (x2 - 2, y2 + h2 / 2 if (h2 := 84) else 0)], lab, kind, num=num)
    return boxes




def begin(width: int, height: int, title: str, subtitle: str) -> None:
    global W, H
    W, H = width, height
    out.clear()
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
               f'font-family="Inter, Helvetica, Arial, sans-serif" font-size="15">')
    out.append('<defs>' + "".join(
        f'<marker id="a-{k}" markerWidth="11" markerHeight="9" refX="10" refY="4.5" orient="auto">'
        f'<path d="M0,0 L11,4.5 L0,9 z" fill="{COL[k][1]}"/></marker>' for k in COL) + '</defs>')
    out.append(f'<rect width="{W}" height="{H}" fill="#fff"/>')
    out.append(f'<text x="30" y="36" font-size="26" font-weight="700" fill="#1c1c3a">{escape(title)}</text>'
               f'<text x="30" y="60" font-size="14" fill="#555">{escape(subtitle)}</text>')


def text(x: float, y: float, s: str, kind: str = "ext", size: float = 13, bold: bool = False,
         anchor: str = "start") -> None:
    out.append(f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" fill="{COL[kind][2]}"'
               f'{" font-weight=\"700\"" if bold else ""}>{escape(s)}</text>')


def panel(x: int, y: int, w: int, h: int, kind: str, title: str, sub: str = "") -> None:
    fill, stroke, tx = COL[kind]
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{fill}" fill-opacity="0.35" '
               f'stroke="{stroke}" stroke-width="1.4"/>')
    text(x + 16, y + 26, title, kind, 17, True)
    if sub:
        text(x + 16, y + 46, sub, kind, 12.5)


def finish(name: str, scale: float = 1.5) -> None:
    out.append("</svg>")
    svg = HERE / f"{name}.svg"
    svg.write_text("\n".join(out))
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "p.html"
        page.write_text(f'<body style="margin:0">{svg.read_text()}</body>')
        subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
                        f"--window-size={W},{H}", f"--force-device-scale-factor={scale}",
                        f"--screenshot={HERE / (name + '.png')}", f"file://{page}"],
                       capture_output=True, timeout=120)
    print(f"drawn {name} {W}x{H}")
