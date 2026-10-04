"""Fixed Chromium dependencies for the supported Ubuntu execution hosts."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

# Chromium's native dependency mapping from Playwright nativeDeps.ts. Keep this
# independent of runtime imports: SSH ships this source beside the host worker.
_LIBRARIES = {
    "libasound.so.2": "libasound2",
    "libatk-1.0.so.0": "libatk1.0-0",
    "libatk-bridge-2.0.so.0": "libatk-bridge2.0-0",
    "libatspi.so.0": "libatspi2.0-0",
    "libcairo.so.2": "libcairo2",
    "libcups.so.2": "libcups2",
    "libdbus-1.so.3": "libdbus-1-3",
    "libdrm.so.2": "libdrm2",
    "libgbm.so.1": "libgbm1",
    "libglib-2.0.so.0": "libglib2.0-0",
    "libgio-2.0.so.0": "libglib2.0-0",
    "libgobject-2.0.so.0": "libglib2.0-0",
    "libgmodule-2.0.so.0": "libglib2.0-0",
    "libnspr4.so": "libnspr4",
    "libplc4.so": "libnspr4",
    "libplds4.so": "libnspr4",
    "libnss3.so": "libnss3",
    "libnssutil3.so": "libnss3",
    "libsmime3.so": "libnss3",
    "libpango-1.0.so.0": "libpango-1.0-0",
    "libwayland-client.so.0": "libwayland-client0",
    "libX11.so.6": "libx11-6",
    "libxcb.so.1": "libxcb1",
    "libXcomposite.so.1": "libxcomposite1",
    "libXdamage.so.1": "libxdamage1",
    "libXext.so.6": "libxext6",
    "libXfixes.so.3": "libxfixes3",
    "libxkbcommon.so.0": "libxkbcommon0",
    "libXrandr.so.2": "libxrandr2",
}
_T64_PACKAGES = frozenset(
    {"libasound2", "libatk1.0-0", "libatk-bridge2.0-0", "libatspi2.0-0", "libcups2", "libglib2.0-0"}
)


def _packages(ubuntu_release: str) -> dict[str, str]:
    if ubuntu_release not in {"22.04", "24.04"}:
        raise ValueError("Chromium system libraries support Ubuntu 22.04 and 24.04")
    return {
        library: package + ("t64" if ubuntu_release == "24.04" and package in _T64_PACKAGES else "")
        for library, package in _LIBRARIES.items()
    }


def chromium_packages(ubuntu_release: str) -> tuple[str, ...]:
    return tuple(sorted(set(_packages(ubuntu_release).values())))


def missing_library_packages(ldd_output: str, ubuntu_release: str) -> tuple[str, ...]:
    """Resolve misses; unknown libraries remain a visible unsupported diagnostic."""
    mapping = _packages(ubuntu_release)
    missing = set(re.findall(r"^\s*(\S+)\s+=>\s+not found\s*$", ldd_output, re.MULTILINE))
    unknown = missing - mapping.keys()
    if unknown:
        raise ValueError("Unmapped Chromium libraries: " + ", ".join(sorted(unknown)))
    return tuple(sorted({mapping[library] for library in missing}))


def apt_install_command(packages: tuple[str, ...]) -> str:
    return shlex.join(("sudo", "apt-get", "install", "-y", "--no-install-recommends", *packages))


def install_system_libraries() -> None:
    """Root install/update only; ordinary browser installation never elevates."""
    from rcp.limits import BROWSER_INSTALL_TIMEOUT_SECONDS

    if os.geteuid() != 0:
        raise PermissionError("Installing Chromium system libraries requires root")
    release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            release[key] = value.strip('"')
    if release.get("ID") != "ubuntu":
        raise ValueError("Chromium system library installation requires Ubuntu")
    packages = chromium_packages(release.get("VERSION_ID", ""))
    try:
        subprocess.run(
            ("/usr/bin/apt-get", "install", "-y", "--no-install-recommends", *packages),
            env={
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "DEBIAN_FRONTEND": "noninteractive",
                "LANG": "C.UTF-8",
            },
            stdout=subprocess.DEVNULL,
            timeout=BROWSER_INSTALL_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "Chromium library installation failed: " + apt_install_command(packages)
        ) from exc


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--install"]:
        raise SystemExit("Usage: python -m rcp.browser.libraries --install")
    install_system_libraries()
