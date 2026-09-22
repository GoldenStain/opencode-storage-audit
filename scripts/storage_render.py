"""Render audit and cleanup reports as aligned, optionally colored terminal text."""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import Callable, Iterable, Sequence


RESET = "\x1b[0m"
CODES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
}
WIDE_CATEGORIES = ("W", "F")
UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def color_enabled(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


class Console:
    """Write styled lines to a stream, disabling color when it is not wanted."""

    def __init__(self, stream=None, color: bool | None = None):
        self.stream = stream if stream is not None else sys.stdout
        self.color = color_enabled(self.stream) if color is None else bool(color)

    def _styled(self, name: str, text: str) -> str:
        if not self.color or not text:
            return text
        return f"\x1b[{CODES[name]}m{text}{RESET}"

    def bold(self, text: str) -> str:
        return self._styled("bold", text)

    def dim(self, text: str) -> str:
        return self._styled("dim", text)

    def red(self, text: str) -> str:
        return self._styled("red", text)

    def green(self, text: str) -> str:
        return self._styled("green", text)

    def yellow(self, text: str) -> str:
        return self._styled("yellow", text)

    def cyan(self, text: str) -> str:
        return self._styled("cyan", text)

    def magenta(self, text: str) -> str:
        return self._styled("magenta", text)

    def write(self, text: str = "") -> None:
        self.stream.write(f"{text}\n")

    def line(self, label: str, value: str, width: int = 14) -> None:
        self.write(f"  {pad(label, width)}{value}")

    def section(self, title: str) -> None:
        self.write("")
        self.write(self.bold(self.cyan(f"▍{title}")))

    def warning(self, text: str) -> None:
        self.write(self.yellow(text))

    def error(self, text: str) -> None:
        self.stream.write(self.red(text) + "\n")


def display_width(text: str) -> int:
    """Return the terminal column width of text, counting East Asian wide glyphs as two."""
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in WIDE_CATEGORIES else 1
    return width


def truncate(text: str, columns: int) -> str:
    if columns <= 0:
        return ""
    if display_width(text) <= columns:
        return text
    result = ""
    used = 0
    for char in text:
        step = display_width(char)
        if used + step > columns - 1:
            break
        result += char
        used += step
    return f"{result}…"


def pad(text: str, columns: int, align: str = "left") -> str:
    gap = max(0, columns - display_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def human_bytes(value: float) -> str:
    """Format a byte count with an automatically chosen binary unit."""
    amount = float(value)
    for unit in UNITS[:-1]:
        if abs(amount) < 1024:
            if unit == "B":
                return f"{amount:.0f} B"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} {UNITS[-1]}"


def human_count(value: int) -> str:
    return f"{value:,}"


def bar(fraction: float, width: int = 18) -> str:
    fraction = min(1.0, max(0.0, fraction))
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)


def ratio_text(fraction: float) -> str:
    return f"{fraction * 100:.1f}%"


def panel(title: str, lines: Sequence[str], console: Console, width: int = 74) -> None:
    """Write a boxed panel whose title and lines are aligned by display width."""
    body = [truncate(line, width) for line in lines]
    inner = max([display_width(title)] + [display_width(line) for line in body])
    inner = min(inner, width)
    title_gap = max(1, inner - display_width(title) - 1)
    console.write(
        console.dim("┌─ ") + console.bold(title) + " " + console.dim("─" * title_gap + "┐")
    )
    for line in body:
        console.write(console.dim("│ ") + pad(line, inner) + console.dim(" │"))
    console.write(console.dim("└" + "─" * (inner + 2) + "┘"))


Column = tuple[str, str, str]
Row = Sequence[object]


class Table:
    """Align rows into columns, padding by display width before applying style."""

    def __init__(self, columns: Iterable[Column]):
        self.columns: list[Column] = list(columns)
        self.rows: list[tuple[list[object], str | None]] = []

    def add(self, *cells: object, below: str | None = None) -> None:
        self.rows.append((list(cells), below))

    def _styler(self, console: Console, name: str) -> Callable[[str], str]:
        return getattr(console, name) if name else lambda text: text

    def widths(self) -> list[int]:
        widths = []
        for index, (header, _align, _style) in enumerate(self.columns):
            width = display_width(header)
            for cells, _below in self.rows:
                width = max(width, display_width(str(cells[index])))
            widths.append(width)
        return widths

    def header(self, console: Console, widths: Sequence[int]) -> str:
        cells = []
        for index, ((name, align, _style), width) in enumerate(zip(self.columns, widths)):
            text = pad(name, width, align)
            if index == len(self.columns) - 1:
                text = text.rstrip()
            cells.append(console.dim(text))
        return "  ".join(cells)

    def lines(self, console: Console, widths: Sequence[int]) -> list[str]:
        rendered = []
        for cells, below in self.rows:
            styled = []
            for index, (cell, (_header, align, style), width) in enumerate(
                zip(cells, self.columns, widths)
            ):
                text = pad(str(cell), width, align)
                if index == len(self.columns) - 1:
                    text = text.rstrip()
                styled.append(self._styler(console, style)(text))
            rendered.append("  ".join(styled).rstrip())
            if below:
                rendered.append(console.dim(f"    {below}"))
        return rendered

    def write(self, console: Console) -> None:
        widths = self.widths()
        console.write(f"  {self.header(console, widths)}")
        for line in self.lines(console, widths):
            console.write(f"  {line}")