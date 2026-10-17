"""Records the scripted demo as a video: `uv run python -m chaos.record [OUT.mp4]`.

Runs the same autopilot as `chaos.demo --auto` against the real lab (keepalived, Traefik, agents,
the control plane), but instead of drawing to a terminal it draws each refresh of the dashboard
into an image and pipes the images to ffmpeg. The picture is the real dashboard reading the real
stack; only the terminal emulator is replaced by a renderer that paints rich's styled cells.
No narration: the caption bar at the bottom says what is being shown.

Needs ffmpeg, Pillow (a dependency of the tests project) and a monospace font with box-drawing
and braille glyphs (JetBrains Mono Nerd Font, DejaVu Sans Mono, ...).
"""

import io
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from rich.console import Console
from rich.segment import Segment

from chaos import tui
from chaos.world import World, boot
from lab import netns
from lab.stack import Postgres

COLS, ROWS = 120, 36
FPS = 10
FONTS = [
    ("/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf",
     "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSansMono.ttf", "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf"),
]
Rgb = tuple[int, int, int]
FALLBACKS = ("/usr/share/fonts/Adwaita/AdwaitaMono-Regular.ttf",
             "/usr/share/fonts/noto/NotoSansSymbols2-Regular.ttf")
BACKGROUND: Rgb = (18, 10, 26)
FOREGROUND: Rgb = (226, 222, 236)


def rgb(color: object) -> Rgb:
    r, g, b = color.get_truecolor()  # type: ignore[attr-defined]
    return int(r), int(g), int(b)


class Painter:
    """Paints rows of styled segments onto an image, one monospace cell per character."""

    def __init__(self, size: int = 22) -> None:
        for regular, bold in FONTS:
            if Path(regular).exists():
                self.regular = ImageFont.truetype(regular, size, layout_engine=ImageFont.Layout.BASIC)
                self.bold = ImageFont.truetype(bold if Path(bold).exists() else regular, size,
                                              layout_engine=ImageFont.Layout.BASIC)
                break
        else:
            raise RuntimeError("no monospace font found; edit FONTS in chaos/record.py")
        self.fallbacks = [ImageFont.truetype(f, size, layout_engine=ImageFont.Layout.BASIC)
                          for f in FALLBACKS if Path(f).exists()]
        self._owner: dict[tuple[int, str], ImageFont.FreeTypeFont] = {}
        self._masks: dict[tuple[str, int], tuple[Image.Image, int, int]] = {}
        box = self.regular.getbbox("M")
        self.cw = round(self.regular.getlength("M"))
        self.ch = round((box[3] - box[1]) * 1.55)
        self.width, self.height = COLS * self.cw, ROWS * self.ch

    def mask(self, run: str, font: ImageFont.FreeTypeFont) -> tuple[Image.Image, int, int]:
        """The glyphs of `run` as a coverage mask and where its corner sits relative to the
        baseline origin; most of the screen repeats from frame to frame, so these are cached."""
        key = (run, id(font))
        hit = self._masks.get(key)
        if hit is None:  # not drawn yet
            left, top, right, bottom = (int(v) for v in font.getbbox(run, anchor="ls"))
            im = Image.new("L", (max(1, right - left), max(1, bottom - top)))
            ImageDraw.Draw(im).text((-left, -top), run, font=font, fill=255, anchor="ls")
            if len(self._masks) > 20000:
                self._masks.clear()
            hit = self._masks[key] = (im, left, top)
        return hit

    @staticmethod
    def _has(font: ImageFont.FreeTypeFont, ch: str) -> bool:
        """Whether `font` draws `ch` as itself and not as its missing-glyph box."""
        def bits(c: str) -> bytes:
            im = Image.new("L", (64, 64))
            ImageDraw.Draw(im).text((4, 48), c, font=font, fill=255, anchor="ls")
            return im.tobytes()

        return bits(ch) != bits("\U0010fffe")

    def font_for(self, base: ImageFont.FreeTypeFont, ch: str) -> ImageFont.FreeTypeFont:
        key = (id(base), ch)
        if key not in self._owner:
            self._owner[key] = next((f for f in [base, *self.fallbacks] if self._has(f, ch)), base)
        return self._owner[key]

    def frame(self, lines: list[list[Segment]]) -> bytes:
        img = Image.new("RGB", (self.width, self.height), BACKGROUND)
        draw = ImageDraw.Draw(img)
        for row, segments in enumerate(lines[:ROWS]):
            col = 0
            for seg in segments:
                text = seg.text
                if not text or seg.control:
                    continue
                style = seg.style
                fg: Rgb = FOREGROUND
                bg: Rgb | None = None
                if style is not None:
                    if style.color is not None and style.color.triplet is not None:
                        fg = rgb(style.color)
                    if style.bgcolor is not None:
                        bg = rgb(style.bgcolor)
                    if style.reverse:
                        fg, bg = (bg or BACKGROUND), fg
                    if style.dim:
                        fg = (int(fg[0] * 0.6 + BACKGROUND[0] * 0.4),
                              int(fg[1] * 0.6 + BACKGROUND[1] * 0.4),
                              int(fg[2] * 0.6 + BACKGROUND[2] * 0.4))
                x, y = col * self.cw, row * self.ch
                if bg is not None:
                    draw.rectangle([x, y, x + len(text) * self.cw - 1, y + self.ch - 1], fill=bg)
                base = self.bold if style is not None and style.bold else self.regular
                baseline = y + round(self.ch * 0.74)
                # runs of characters from one font are drawn in one call; a cell is a cell wide
                start = 0
                while start < len(text):
                    font = self.font_for(base, text[start])
                    end = start + 1
                    while end < len(text) and self.font_for(base, text[end]) is font:
                        end += 1
                    run = text[start:end]
                    if run.strip():
                        mask, dx, dy = self.mask(run, font)
                        img.paste(fg, (x + start * self.cw + dx, baseline + dy), mask)
                    start = end
                col += len(text)
        return img.tobytes()


def title_card(painter: Painter, lines: list[str]) -> bytes:
    img = Image.new("RGB", (painter.width, painter.height), BACKGROUND)
    draw = ImageDraw.Draw(img)
    y = painter.height // 2 - len(lines) * painter.ch // 2
    for i, text in enumerate(lines):
        font = painter.bold if i == 0 else painter.regular
        w = font.getlength(text)
        draw.text(((painter.width - w) / 2, y + i * painter.ch * 1.6), text, font=font,
                  fill=(255, 190, 90) if i == 0 else FOREGROUND)
    return img.tobytes()


class Recorder:
    def __init__(self, out: Path, painter: Painter) -> None:
        self.painter = painter
        self.ff = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{painter.width}x{painter.height}", "-r", str(FPS), "-i", "-",
             "-c:v", "libx264", "-preset", "medium", "-crf", "24", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
        self.written = 0
        self.t0 = time.monotonic()

    def hold(self, frame: bytes, seconds: float) -> None:
        assert self.ff.stdin
        for _ in range(max(1, round(seconds * FPS))):
            self.ff.stdin.write(frame)
            self.written += 1

    def catch_up(self, frame: bytes) -> None:
        """Write `frame` for as long as real time has run past what has been written."""
        assert self.ff.stdin
        due = round((time.monotonic() - self.t0) * FPS)
        for _ in range(max(1, due - self.written)):
            self.ff.stdin.write(frame)
            self.written += 1

    def close(self) -> None:
        assert self.ff.stdin
        self.ff.stdin.close()
        self.ff.wait()


def record(w: World, out: Path) -> float:
    painter = Painter()
    rec = Recorder(out, painter)
    console = Console(file=io.StringIO(), width=COLS, height=ROWS, force_terminal=True,
                      color_system="truecolor")
    dash = tui.Dash()
    ctl = tui.Controller(w, dash)
    ctl.start()
    dash.log("booted: two gateways, a sandbox backend, the control plane", "ok")
    rec.hold(title_card(painter, [
        "Resilient IP Optimizer",
        "",
        "real keepalived + Traefik + ipo-agent on two gateways, the real control plane",
        "subdomain routing · hot reload · 12 teams at once · expiry · failover · split brain",
    ]), 4)
    rec.t0 = time.monotonic() - rec.written / FPS
    threading.Thread(target=tui.autopilot, args=(ctl,), daemon=True).start()
    try:
        while not dash.quit:
            layout = tui.render(dash, (COLS, ROWS))
            lines = console.render_lines(layout, console.options.update(width=COLS, height=ROWS),
                                         pad=True)
            rec.catch_up(painter.frame(lines))
            time.sleep(0.05)
    finally:
        ctl.close()
    rec.hold(title_card(painter, ["Resilient, hot-reloadable, isolated per team.", "",
                                  "docs/reports/benchmarks.md"]), 3)
    rec.close()
    return rec.written / FPS


def main(argv: list[str]) -> int:
    out = Path(argv[0] if argv else "demo.mp4").resolve()
    work = Path(tempfile.mkdtemp(prefix="ipo-record-"))
    pg = Postgres(work / "pg")
    pg.start()
    try:
        import os
        env = {**os.environ, "IPO_LAB_DSN": pg.dsn, "IPO_LAB_WORKDIR": str(work / "lab"),
               "IPO_RECORD_OUT": str(out)}
        cmd = netns.in_lab([sys.executable, "-m", "chaos.record", "--inner"])
        return subprocess.run(cmd, env=env, cwd=Path(__file__).parents[1]).returncode
    finally:
        pg.stop()


def inner() -> int:
    import os
    w = boot(Path(os.environ["IPO_LAB_WORKDIR"]), os.environ["IPO_LAB_DSN"])
    try:
        seconds = record(w, Path(os.environ["IPO_RECORD_OUT"]))
        print(f"recorded {seconds:.0f} s to {os.environ['IPO_RECORD_OUT']}")
    finally:
        w.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(inner() if sys.argv[1:2] == ["--inner"] else main(sys.argv[1:]))
