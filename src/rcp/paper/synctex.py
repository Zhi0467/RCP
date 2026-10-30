"""Source line to PDF position, read from one compile's SyncTeX output."""

from __future__ import annotations

import gzip
import os
import re
from pathlib import Path

from rcp.limits import PAPER_SYNCTEX_MAX_BYTES

# One line's first mark in the PDF: (source line, page, y in points from the page top).
SyncTexMap = dict[str, list[tuple[int, int, float]]]

_RECORD = re.compile(r"[\[(hvgkx$](\d+),(\d+)(?:,-?\d+)?:(-?\d+),(-?\d+)")
# SyncTeX counts TeX scaled points, 65536 to a TeX point and 72.27 TeX points to
# the inch; the preview scales pages in PDF points, 72 to the inch.
_SCALED_POINTS_PER_PDF_POINT = 65536 * 72.27 / 72


def read_synctex(path: Path, tree: Path) -> SyncTexMap | None:
    """Parse a `.synctex.gz`; None when it exceeds its bound."""
    with gzip.open(path, "rb") as stream:
        data = stream.read(PAPER_SYNCTEX_MAX_BYTES + 1)
    if len(data) > PAPER_SYNCTEX_MAX_BYTES:
        return None
    return synctex_map(data.decode("utf-8", errors="replace"), tree)


def synctex_map(text: str, tree: Path) -> SyncTexMap:
    """Map each paper file's lines to where they first appear, keyed by tree-relative path."""
    root = os.path.realpath(tree)
    inputs: dict[str, str] = {}
    unit = 1.0
    magnification = 1000.0
    y_offset = 0.0
    page = 0
    shipout: tuple[str, str] | None = None
    first: dict[tuple[str, int], tuple[int, float]] = {}
    for line in text.splitlines():
        if line.startswith("Input:"):
            tag, _, name = line[len("Input:") :].partition(":")
            if name:
                resolved = os.path.realpath(name)
                if resolved.startswith(root + os.sep):
                    inputs[tag] = Path(resolved).relative_to(root).as_posix()
        elif line.startswith("Unit:"):
            unit = float(line[len("Unit:") :])
        elif line.startswith("Magnification:"):
            magnification = float(line[len("Magnification:") :])
        elif line.startswith("Y Offset:"):
            y_offset = float(line[len("Y Offset:") :])
        elif line.startswith("{"):
            page = int(line[1:])
            shipout = None
        elif page and (record := _RECORD.match(line)):
            tag, number, _x, y = record.groups()
            # A page's first box names the line TeX was reading when it shipped
            # the page; its header, footer, and frame carry that line too, though
            # the line's own text lands on a later page.
            shipout = shipout or (tag, number)
            if (tag, number) == shipout:
                continue
            path = inputs.get(tag)
            if path is None:
                continue
            points = (
                (int(y) * unit + y_offset) * magnification / 1000 / _SCALED_POINTS_PER_PDF_POINT
            )
            key = (path, int(number))
            if key not in first or (page, points) < first[key]:
                first[key] = (page, points)
    result: SyncTexMap = {}
    for (path, number), (where, points) in sorted(first.items()):
        result.setdefault(path, []).append((number, where, round(points, 1)))
    return result
