"""The app's appearance as artifact surfaces see it, from one table.

The viewer shell paints its own chrome with these values, an inline artifact frame
receives them as `--rcp-*` custom properties, and the agent's prompt lists the same
names. A theme the shell cannot learn falls back to the app's default, Aqua light.
"""

from __future__ import annotations

from typing import Literal, cast

ArtifactTheme = Literal["aqua", "classic"]
ArtifactColorMode = Literal["light", "dark"]

DEFAULT_THEME: ArtifactTheme = "aqua"
DEFAULT_COLOR_MODE: ArtifactColorMode = "light"

# What each token is for, in the words the agent's prompt uses.
ARTIFACT_THEME_TOKENS: dict[str, str] = {
    "ink": "body text",
    "muted": "secondary text, axis labels, captions",
    "paper": "the reply's own background",
    "panel": "a raised card or tooltip surface",
    "field": "an input, track, or inset well",
    "rule": "borders, gridlines, dividers",
    "accent": "the primary action and the first data series",
    "accent-ink": "text drawn on the accent color",
    "series-2": "second data series",
    "series-3": "third data series",
    "series-4": "fourth data series",
    "series-5": "fifth data series",
    "series-6": "sixth data series",
    "good": "success or improvement",
    "bad": "failure or regression",
    "radius": "corner radius for cards and controls",
    "font": "the reply's font stack",
    "mono": "a monospace font stack for numbers and code",
}

_AQUA_FONT = '"Helvetica Neue", Helvetica, Arial, sans-serif'
_CLASSIC_FONT = '"Avenir Next", Avenir, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
_MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace'

# Mirrors the app's palettes in web/src/themes/aqua.css and web/src/styles/01-base.css.
_PALETTES: dict[ArtifactTheme, dict[ArtifactColorMode, dict[str, str]]] = {
    "aqua": {
        "light": {
            "ink": "#303944",
            "muted": "#5b6673",
            "paper": "#e5e8ec",
            "panel": "#f1f3f5",
            "field": "#e9edf1",
            "rule": "#bec7d0",
            "accent": "#386792",
            "accent-ink": "#ffffff",
            "series-2": "#326d72",
            "series-3": "#886014",
            "series-4": "#a8483e",
            "series-5": "#785a7c",
            "series-6": "#38644e",
            "good": "#38644e",
            "bad": "#a8483e",
            "radius": "8px",
            "font": _AQUA_FONT,
            "mono": _MONO,
        },
        "dark": {
            "ink": "#e5ebf2",
            "muted": "#afbccc",
            "paper": "#282e36",
            "panel": "#343c46",
            "field": "#29313a",
            "rule": "#485360",
            "accent": "#8bbbe8",
            "accent-ink": "#1c2936",
            "series-2": "#83c4c3",
            "series-3": "#ddc181",
            "series-4": "#efaa9c",
            "series-5": "#d0b1d9",
            "series-6": "#97c6ac",
            "good": "#97c6ac",
            "bad": "#efaa9c",
            "radius": "8px",
            "font": _AQUA_FONT,
            "mono": _MONO,
        },
    },
    "classic": {
        "light": {
            "ink": "#2b251f",
            "muted": "#6b6258",
            "paper": "#f7f0e3",
            "panel": "#fffdf7",
            "field": "#fffdf7",
            "rule": "#d8ccbb",
            "accent": "#873b30",
            "accent-ink": "#fffaf0",
            "series-2": "#2f6f70",
            "series-3": "#b08a2e",
            "series-4": "#3f4e79",
            "series-5": "#7a4166",
            "series-6": "#284b3a",
            "good": "#284b3a",
            "bad": "#bc5545",
            "radius": "4px",
            "font": _CLASSIC_FONT,
            "mono": _MONO,
        },
        "dark": {
            "ink": "#ece3d5",
            "muted": "#a99e8e",
            "paper": "#191512",
            "panel": "#24201b",
            "field": "#221d19",
            "rule": "#3a332c",
            "accent": "#d97b68",
            "accent-ink": "#191512",
            "series-2": "#6fb3ad",
            "series-3": "#e0b95e",
            "series-4": "#8b9bd4",
            "series-5": "#c98bb4",
            "series-6": "#7fb894",
            "good": "#7fb894",
            "bad": "#e08a76",
            "radius": "4px",
            "font": _CLASSIC_FONT,
            "mono": _MONO,
        },
    },
}

# The shell's own chrome also needs relief the agent never sees.
_SHELL_EXTRAS: dict[ArtifactTheme, dict[ArtifactColorMode, str]] = {
    "aqua": {
        "light": "--shadow:1px 14px 30px rgb(61 77 96/22%),0 2px 5px rgb(61 77 96/12%);"
        "--raised:inset 1px 1px 0 rgb(255 255 255/92%),2px 2px 5px rgb(61 77 96/16%);",
        "dark": "--shadow:0 14px 30px rgb(0 0 0/45%);"
        "--raised:inset 1px 1px 0 rgb(204 224 244/20%),2px 2px 5px rgb(0 0 0/35%);",
    },
    "classic": {
        "light": "--shadow:0 18px 52px rgb(65 46 29/14%);--raised:none;",
        "dark": "--shadow:0 18px 52px rgb(0 0 0/55%);--raised:none;",
    },
}


def artifact_theme(value: str | None) -> ArtifactTheme:
    return cast(ArtifactTheme, value) if value in _PALETTES else DEFAULT_THEME


def artifact_color_mode(value: str | None) -> ArtifactColorMode:
    return cast(ArtifactColorMode, value) if value in {"light", "dark"} else DEFAULT_COLOR_MODE


def theme_tokens(theme: ArtifactTheme, mode: ArtifactColorMode) -> dict[str, str]:
    return dict(_PALETTES[theme][mode])


def shell_theme_css() -> str:
    """The viewer shell's tokens for every theme and mode, keyed by root attributes."""

    rules = []
    for theme, modes in _PALETTES.items():
        for mode, palette in modes.items():
            selector = ":root" if theme == DEFAULT_THEME else f':root[data-theme="{theme}"]'
            if mode == "dark":
                selector += '[data-color-mode="dark"]'
            declarations = (
                f"--paper:{palette['paper']};--panel:{palette['panel']};"
                f"--field:{palette['field']};--ink:{palette['ink']};--muted:{palette['muted']};"
                f"--rule:{palette['rule']};--accent:{palette['accent']};"
                f"--accent-ink:{palette['accent-ink']};--radius:{palette['radius']};"
                f"--ui:{palette['font']};{_SHELL_EXTRAS[theme][mode]}color-scheme:{mode}"
            )
            rules.append(f"{selector}{{{declarations}}}")
    return "\n".join(rules)


def artifact_theme_css(theme: ArtifactTheme, mode: ArtifactColorMode) -> str:
    """Zero-specificity defaults an inline frame paints with until its own CSS speaks.

    Every frame in the chain must declare the same `color-scheme`, or the browser
    paints an opaque backdrop behind the nested document; a page that declares its
    own scheme keeps it and is shown on that backdrop.
    """

    palette = _PALETTES[theme][mode]
    tokens = "".join(f"--rcp-{name}:{value};" for name, value in palette.items())
    return (
        f":root{{{tokens}--rcp-color-mode:{mode};color-scheme:{mode}}}"
        ":where(html){color:var(--rcp-ink);font:14px/1.55 var(--rcp-font)}"
        ":where(body){margin:0}"
    )
