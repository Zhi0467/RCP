"""Build explicitly synthetic RCP artifacts for disposable reboot qualification.

Both wheels come from the workflow checkout. Only the target's private build
copy receives one real, forward-only SQLite migration. These artifacts are
never published or usable as a promoted production release.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
from pathlib import Path

from tests.supervisor_reboot_vm import run

BASE_BUILD = 900001
TARGET_BUILD = 900002
MIGRATION_NAME = "supervisor_qualification_v1"
MIGRATION_TABLE = "supervisor_qualification"


def add_forward_migration(path: Path) -> int:
    source = path.read_text()
    module = ast.parse(source)
    owner = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "AppStoreBase"
    )
    registry = next(
        node
        for node in owner.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_STORAGE_SCHEMA_MIGRATIONS"
    )
    entries = ast.literal_eval(registry.value)
    if any(name == MIGRATION_NAME for _, name in entries):
        raise ValueError("The qualification migration is already present.")
    version = entries[-1][0] + 1
    lines = source.splitlines(keepends=True)
    assert registry.end_lineno is not None
    lines.insert(registry.end_lineno - 1, f'        ({version}, "{MIGRATION_NAME}"),\n')
    source = "".join(lines)
    boundary = "        if schema_capture is not None:\n"
    if source.count(boundary) != 1:
        raise ValueError(
            "The storage migration dispatch boundary changed; review the live fixture."
        )
    injection = (
        "        self._run_storage_schema_migration(\n"
        "            connection,\n"
        f"            version={version},\n"
        f"            name={MIGRATION_NAME!r},\n"
        "            migration=lambda db: db.execute(\n"
        f"                'CREATE TABLE {MIGRATION_TABLE} (value TEXT NOT NULL)'\n"
        "            ),\n"
        "        )\n"
    )
    modified = source.replace(boundary, injection + boundary)
    compile(modified, str(path), "exec")
    path.write_text(modified)
    return version


def build_bundles(workspace: Path, output: Path) -> dict:
    if output.exists():
        raise ValueError("Qualification output must be a new directory.")
    if not (workspace / "web" / "dist" / "index.html").is_file():
        raise ValueError("Build web/dist before preparing qualification wheels.")
    output.mkdir(mode=0o700, parents=True)
    supervisor_dist = output / "supervisor-dist"
    run(
        [
            "uv",
            "build",
            "--wheel",
            "--project",
            str(workspace / "supervisor"),
            "--out-dir",
            str(supervisor_dist),
        ],
        timeout=300,
    )
    (supervisor_wheel,) = supervisor_dist.glob("*.whl")
    dependencies = run(
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project"], cwd=workspace, timeout=300
    ).stdout
    supervisor_dependencies = run(
        [
            "uv",
            "export",
            "--project",
            str(workspace / "supervisor"),
            "--frozen",
            "--no-dev",
            "--no-emit-project",
        ],
        timeout=300,
    ).stdout
    receipt = {
        "synthetic_qualification_artifacts": True,
        "source_revision": run(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip(),
        "source_tree_dirty": bool(run(["git", "status", "--porcelain"], cwd=workspace).stdout),
        "migration_name": MIGRATION_NAME,
        "bundles": {},
    }
    for name, build, commit in (
        ("base", BASE_BUILD, "1111111"),
        ("target", TARGET_BUILD, "2222222"),
    ):
        source = output / f"{name}-source"
        source.mkdir()
        for filename in ("pyproject.toml", "uv.lock", "README.md"):
            shutil.copyfile(workspace / filename, source / filename)
        shutil.copytree(
            workspace / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        shutil.copytree(workspace / "web" / "dist", source / "web" / "dist")
        if name == "target":
            receipt["target_ledger_head"] = add_forward_migration(
                source / "src" / "rcp" / "storage" / "base.py"
            )
        # Use the same production stamping implementation on a disposable copy.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "qualification_release_build", workspace / "packaging" / "release_build.py"
        )
        assert spec is not None and spec.loader is not None
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        version = helper.stamp_version(
            str(build), commit, version_file=source / "src" / "rcp" / "__init__.py"
        )
        bundle = output / name
        run(
            ["uv", "build", "--wheel", "--project", str(source), "--out-dir", str(bundle)],
            timeout=300,
        )
        (bundle / ".gitignore").unlink(missing_ok=True)
        shutil.copyfile(supervisor_wheel, bundle / supervisor_wheel.name)
        (bundle / "requirements.lock.txt").write_text(dependencies)
        (bundle / "supervisor-requirements.lock.txt").write_text(supervisor_dependencies)
        helper.check_assets(bundle, require_supervisor=True)
        helper.write_manifest(bundle, Path("manifest.sha256"))
        manifest = bundle / "manifest.sha256"
        receipt["bundles"][name] = {
            "build": build,
            "version": version,
            "commit": commit,
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        }
        shutil.rmtree(source)
    (output / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(build_bundles(arguments.workspace.resolve(), arguments.output.resolve())))


if __name__ == "__main__":
    main()
