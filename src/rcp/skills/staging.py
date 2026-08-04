from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from rcp.skill_registry import SkillRegistry, SkillSelection, official_registry
from rcp.transport import RemoteRunStage


def stage_skill_selection(
    selection: SkillSelection,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    label: str,
) -> list[dict[str, object]]:
    """Stage one resolved official selection and return prompt-safe pointers.

    The source-controlled package directories are copied into the run stage as
    immutable inputs. Every attempt stages its own bundle under its own label,
    including a retry or a resume that reuses the stage folder: the registry is
    authoritative at launch, so the bytes an attempt reports are always the
    bytes it was given. A reused stage keeps the earlier bundles until the
    retention sweep reclaims the whole folder.
    """

    if (local_stage is None) == (remote_stage is None):
        raise ValueError("exactly one task stage must be selected")
    if not label or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
        for character in label
    ):
        raise ValueError("skill staging label contains unsupported characters")
    if not selection.resolved_skill_packages:
        return []

    registry = official_registry()
    if remote_stage is not None:
        if remote_stage.root is None:
            raise RuntimeError("remote run stage is not open")
        with tempfile.TemporaryDirectory(prefix="rcp-skill-bundle-") as temporary:
            source_bundle = Path(temporary)
            _copy_packages(registry, selection, source_bundle)
            remote_stage.put_directory(source_bundle, label)
        return _pointers(registry, selection, remote_stage.root / "inputs" / label)

    assert local_stage is not None
    inputs = local_stage / "inputs"
    inputs.mkdir(mode=0o700, parents=True, exist_ok=True)
    bundle = inputs / label
    if os.path.lexists(bundle):
        raise ValueError("immutable skill staging bundle already exists")
    bundle.mkdir(mode=0o700)
    _copy_packages(registry, selection, bundle)
    _protect_tree(bundle)
    return _pointers(registry, selection, bundle)


def _copy_packages(registry: SkillRegistry, selection: SkillSelection, destination: Path) -> None:
    for reference in selection.resolved_skill_packages:
        source = registry.package_path(reference)
        target = destination / reference.kind / reference.id
        target.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copytree(source, target, symlinks=False)


def _protect_tree(root: Path) -> None:
    for directory, _children, files in os.walk(root):
        Path(directory).chmod(0o500)
        for filename in files:
            path = Path(directory) / filename
            if path.is_symlink() or not path.is_file():
                raise ValueError("official skill staging contains a non-regular file")
            path.chmod(0o400)


def _pointers(
    registry: SkillRegistry,
    selection: SkillSelection,
    bundle: Path | PurePosixPath,
) -> list[dict[str, object]]:
    pointers: list[dict[str, object]] = []
    for reference in selection.resolved_skill_packages:
        package = registry.package(reference.kind, reference.id)
        dependencies = ", ".join(f"{item.id}@{item.version}" for item in package.dependencies)
        pointers.append(
            {
                "id": reference.id,
                "kind": reference.kind,
                "label": package.label,
                "description": package.description,
                "version": reference.version,
                "path": str(bundle / reference.kind / reference.id),
                "dependencies": dependencies,
            }
        )
    return pointers
