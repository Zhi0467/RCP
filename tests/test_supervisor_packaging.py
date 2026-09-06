from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path


def test_supervisor_wheel_builds_and_runs_without_rcp_or_web(tmp_path: Path) -> None:
    """Build only the subproject, then exercise its installed entry point in isolation."""
    source = Path(__file__).resolve().parents[1] / "supervisor"
    project = tmp_path / "supervisor"
    shutil.copytree(source, project, ignore=shutil.ignore_patterns(".venv", "__pycache__", "dist"))
    environment = {
        key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    uv = shutil.which("uv")
    assert uv is not None, "the project's uv package manager is required for wheel verification"

    def run(*command: str) -> str:
        result = subprocess.run(
            command, cwd=tmp_path, env=environment, text=True, capture_output=True, timeout=120
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    run(uv, "lock", "--project", str(project), "--check")
    lock_export = run(
        uv, "export", "--project", str(project), "--frozen", "--no-dev", "--no-emit-project"
    )
    assert not [line for line in lock_export.splitlines() if line and not line.startswith("#")]
    run(uv, "build", str(project), "--wheel", "--out-dir", str(tmp_path / "dist"))
    wheels = list((tmp_path / "dist").glob("*.whl"))
    assert len(wheels) == 1
    run(sys.executable, "-m", "venv", "--without-pip", str(tmp_path / "isolated"))
    python = tmp_path / "isolated" / "bin" / "python"
    run(uv, "pip", "install", "--python", str(python), "--offline", "--no-deps", str(wheels[0]))
    run(
        str(python),
        "-I",
        "-c",
        textwrap.dedent(
            """
            import importlib
            import importlib.abc
            import importlib.metadata
            import importlib.util
            import pkgutil
            import sys

            assert importlib.util.find_spec("rcp") is None
            class RefuseRcp(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "rcp" or fullname.startswith("rcp."):
                        raise AssertionError(f"supervisor imports application module {fullname}")
            sys.meta_path.insert(0, RefuseRcp())
            import rcp_supervisor
            for module in pkgutil.walk_packages(rcp_supervisor.__path__, "rcp_supervisor."):
                importlib.import_module(module.name)
            distribution = importlib.metadata.distribution("rcp-supervisor")
            assert distribution.version == rcp_supervisor.__version__
            assert "+" not in distribution.version
            assert not distribution.requires
            entry, = distribution.entry_points
            assert entry.name == "rcp-supervisor"
            assert entry.value == "rcp_supervisor.cli:main"
            entry.load()
            """
        ),
    )
    assert "usage:" in run(str(python.parent / "rcp-supervisor"), "--help").lower()
