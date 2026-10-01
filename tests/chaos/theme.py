"""One palette and a few drawing helpers for every screen of the demo.

The world is FelCloud's own: deep violet for the frame, amber for the one thing happening now,
mint for what is done or healthy, coral for what broke. The two gateways are the only other
hues (teal and orchid), because a request stream needs two colours that never read as state.
Secondary text is violet-tinted, never grey. Backgrounds are left to the terminal except for
the header band and badges, so the screen works on a light or a dark theme.
"""

from __future__ import annotations

import time

from rich import box
from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

AMBER, MINT, CORAL = "#ffc914", "#5be3a1", "#ff5d6c"
TEXT, DIM, EDGE, VIOLET = "#ece6f5", "#9487ad", "#6a4f94", "#2e0a40"
TEAL, ORCHID = "#4fd1c5", "#e08cf5"
GW_COLOURS = {"gw-a": TEAL, "gw-b": ORCHID}
STATE_COLOURS = {"MASTER": MINT, "BACKUP": AMBER, "FAULT": CORAL, "DOWN": CORAL, "UNKNOWN": DIM}

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PARTIALS = " ▏▎▍▌▋▊▉"


def spinner(now: float) -> str:
    return SPINNER[int(now * 12) % len(SPINNER)]


def bar(fraction: float, width: int, colour: str = MINT, rest: str = EDGE) -> Text:
    """A bar with eighth-cell resolution, so a slow fill still visibly moves."""
    fraction = max(0.0, min(1.0, fraction))
    eighths = round(fraction * width * 8)
    full, part = divmod(eighths, 8)
    out = Text("█" * full, style=colour)
    if part and full < width:
        out.append(PARTIALS[part], style=colour)
        full += 1
    out.append("▁" * (width - full), style=rest)
    return out


def badge(label: str, colour: str) -> Text:
    return Text(f" {label} ", style=f"bold #120a1a on {colour}")


def dim(text: str) -> Text:
    return Text(text, style=DIM)


def track(stages: list[str], current: int) -> Text:
    """build ▸ operate ▸ break ▸ teardown, with where we are in the story."""
    out = Text()
    for i, name in enumerate(stages):
        if i:
            out.append("  ▸  ", style=EDGE)
        if i < current:
            out.append(name, style=f"{MINT}")
        elif i == current:
            out.append(name, style=f"bold {AMBER}")
        else:
            out.append(name, style=DIM)
    return out


def header(stages: list[str], current: int, right: str = "", width: int = 100) -> Panel:
    """The one solid band on the screen: wordmark, the story so far, and a clock or status."""
    left = Text.assemble((" FelCloud ", f"bold #120a1a on {AMBER}"), ("  × IPO   ", f"bold {TEXT}"))
    left.append_text(track(stages, current))
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(justify="right")
    grid.add_row(left, Text(right + " ", style=f"bold {TEXT}"))
    return Panel(grid, style=f"on {VIOLET}", border_style=EDGE, box=box.HEAVY, padding=(0, 0))


def panel(body: RenderableType, title: str, border: str = EDGE, subtitle: str = "",
          heavy: bool = False) -> Panel:
    sub = f"[{DIM}]{subtitle}[/]" if subtitle else None
    return Panel(body, title=f"[bold {TEXT}]{title}[/]", subtitle=sub, border_style=border,
                 box=box.HEAVY if heavy else box.ROUNDED, padding=(0, 1))


def keys(pairs: list[tuple[str, str]], busy: str = "") -> Panel:
    body = Text()
    for k, label in pairs:
        body.append(f" {k} ", style=f"bold #120a1a on {TEXT}")
        body.append(f" {label} ", style=DIM)
    if busy:
        body.append(f"  {busy}", style=f"bold {AMBER}")
    return Panel(body, border_style=EDGE, box=box.SQUARE, padding=(0, 0))


def clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def now() -> float:
    return time.monotonic()
