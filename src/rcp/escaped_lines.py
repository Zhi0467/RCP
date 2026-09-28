from __future__ import annotations

import html


def escaped_lines(text: str, start_line: int = 1, selected_line: int | None = None) -> str:
    lines = text.split("\n")
    if selected_line is not None and not start_line <= selected_line < start_line + len(lines):
        raise ValueError("Requested line is outside the source")
    return "\n".join(
        f'<span id="L{number}" class="line{" selected" if number == selected_line else ""}">'
        f"{html.escape(value, quote=True)}</span>"
        for number, value in enumerate(lines, start=start_line)
    )
