from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
ACTIVE_ACCEPTANCE = DOCS / "acceptance"
ARCHIVED_ACCEPTANCE = DOCS / "archive" / "acceptance"

EXPECTED_SPECS = {
    "api-web-and-desktop-projections.md",
    "authority-and-proposals.md",
    "auto-research-and-branch-merge.md",
    "conversations-episodes-and-watchers.md",
    "graph-history-and-transitions.md",
    "paper-artifacts-and-result-views.md",
    "projects-spaces-and-operations.md",
    "providers-and-containment.md",
}

EXPECTED_CURRENT_FILES = {
    "docs/acceptance/README.md",
    "docs/acceptance/S125-auto-research-graph-branch-merge.md",
    "docs/decisions/README.md",
    "docs/design.md",
    "docs/handoffs/README.md",
    *(f"docs/specs/{name}" for name in EXPECTED_SPECS),
    "tests/test_documentation.py",
}

EXPECTED_ARCHIVED_DESIGN_FILES = {
    "identity-permissions-and-agent-profiles.md",
    "research-control-panel-blueprint-v0.64-pre-modular.md",
    "spaces-and-project-homes.md",
    "team-api-compatibility.md",
    "team-authentication-and-membership.md",
    "team-modules-README.md",
    "team-server-operations.md",
}

REQUIRED_IMPLEMENTATION_FILES = {
    "src/rcp/agents/branch_merge_prompt.py",
    "src/rcp/agents/write_scope.py",
    "src/rcp/core/operations.py",
    "src/rcp/core/transition_models.py",
    "src/rcp/core/transitions.py",
    "src/rcp/history/branches.py",
    "src/rcp/runs/branch_merge.py",
    "src/rcp/runs/branch_merge_request.py",
    "src/rcp/runs/branch_merge_task.py",
    "src/rcp/runs/transition_event_reconciliation.py",
    "web/src/experimentGuidance.ts",
    "web/src/projectTransition.ts",
}

EXPECTED_2026_08_17_HANDOFFS = {
    "handoff-2026-08-17-auto-research-graph-branches.md",
    "handoff-2026-08-17-documentation-model-and-archive.md",
    "handoff-2026-08-17-evidence-assessments.md",
    "handoff-2026-08-17-graph-transition-manager-implementation.md",
    "handoff-2026-08-17-graph-transition-manager.md",
    "handoff-2026-08-17-project-write-containment.md",
    "handoff-2026-08-17-typed-graph-operations.md",
}

EXPECTED_DISPATCH_BUNDLE = EXPECTED_2026_08_17_HANDOFFS - {
    "handoff-2026-08-17-graph-transition-manager.md"
} | {
    "README.md",
    "master-prompt-implement-2026-08-17-design-handoffs.md",
}

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]*]\(([^)]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
INDEX_ROW = re.compile(r"^\| \[(S\d+)]\((S\d+[^)]*\.md)\) \| .* \| ([^|]+) \| ([^|]+) \|$")
EVIDENCE_PATH = re.compile(r"(?:tests|web|packaging|src)/[A-Za-z0-9_./-]+")


def _current_markdown() -> list[Path]:
    files = [ROOT / "AGENTS.md", ROOT / "README.md"]
    files.extend(
        path for path in DOCS.rglob("*.md") if "archive" not in path.relative_to(DOCS).parts
    )
    return sorted(files)


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text().splitlines()
    assert lines and lines[0] == "---", f"{path} has no frontmatter"
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError(f"{path} has unterminated frontmatter") from exc
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if line and not line[0].isspace() and ":" in line:
            key, value = line.split(":", 1)
            values[key] = value.strip()
    return values


def _frontmatter_list(path: Path, key: str) -> list[str]:
    lines = path.read_text().splitlines()
    assert lines and lines[0] == "---", f"{path} has no frontmatter"
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError(f"{path} has unterminated frontmatter") from exc

    prefix = f"{key}:"
    for index, line in enumerate(lines[1:end], start=1):
        if not line.startswith(prefix):
            continue
        inline = line.removeprefix(prefix).strip()
        values = (
            [] if not inline or inline == "none" else [part.strip() for part in inline.split(",")]
        )
        for item in lines[index + 1 : end]:
            if item.startswith("  - "):
                values.append(item.removeprefix("  - ").strip())
            elif item and not item[0].isspace():
                break
        return values
    raise AssertionError(f"{path} lacks {key}")


def _heading_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in path.read_text().splitlines():
        match = HEADING.match(line)
        if match is None:
            continue
        heading = re.sub(r"<[^>]+>", "", match.group(1))
        heading = re.sub(r"[`*_~]", "", heading).lower()
        slug = re.sub(r"[^\w\- ]", "", heading)
        slug = re.sub(r"\s", "-", slug).strip("-")
        duplicate = counts.get(slug, 0)
        counts[slug] = duplicate + 1
        anchors.add(slug if duplicate == 0 else f"{slug}-{duplicate}")
    return anchors


def _local_links(path: Path) -> list[str]:
    links: list[str] = []
    fenced = False
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            links.extend(MARKDOWN_LINK.findall(line))
    return links


def test_current_documentation_layout_and_archives_are_complete() -> None:
    missing_current = [path for path in EXPECTED_CURRENT_FILES if not (ROOT / path).is_file()]
    assert not missing_current, f"missing current documentation: {sorted(missing_current)}"

    assert (DOCS / "design.md").is_file()
    assert not (DOCS / "research-control-panel-blueprint.md").exists()
    assert not (DOCS / "design").exists()
    assert {path.name for path in (DOCS / "specs").glob("*.md")} == EXPECTED_SPECS

    blueprint = (
        DOCS / "archive" / "design" / "research-control-panel-blueprint-v0.64-pre-modular.md"
    )
    assert blueprint.is_file()
    assert "**Version:** 0.64" in blueprint.read_text()
    assert {
        path.name for path in (DOCS / "archive" / "design").glob("*.md")
    } >= EXPECTED_ARCHIVED_DESIGN_FILES
    assert (ARCHIVED_ACCEPTANCE / "README.md").is_file()

    archived_handoffs = DOCS / "archive" / "handoffs"
    assert {
        path.name for path in archived_handoffs.glob("handoff-2026-08-17-*.md")
    } >= EXPECTED_2026_08_17_HANDOFFS
    bundle = archived_handoffs / "rcp_dispatch_handoffs_2026-08-17"
    assert {path.name for path in bundle.glob("*.md")} == EXPECTED_DISPATCH_BUNDLE

    for handoff in EXPECTED_DISPATCH_BUNDLE - {
        "README.md",
        "master-prompt-implement-2026-08-17-design-handoffs.md",
    }:
        assert (bundle / handoff).read_bytes() == (archived_handoffs / handoff).read_bytes()


def test_required_implementation_inventory_is_present() -> None:
    missing = [path for path in REQUIRED_IMPLEMENTATION_FILES if not (ROOT / path).is_file()]
    assert not missing, f"missing implementation files from exported snapshot: {sorted(missing)}"


def test_current_markdown_links_and_anchors_resolve() -> None:
    failures: list[str] = []
    for source in _current_markdown():
        for raw_target in _local_links(source):
            target = raw_target.strip().strip("<>")
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_text, _, fragment = target.partition("#")
            destination = source if not path_text else (source.parent / path_text).resolve()
            if not destination.exists():
                failures.append(f"{source.relative_to(ROOT)} -> {raw_target} (missing)")
                continue
            if (
                fragment
                and destination.is_file()
                and destination.suffix == ".md"
                and fragment not in _heading_anchors(destination)
            ):
                failures.append(f"{source.relative_to(ROOT)} -> {raw_target} (missing anchor)")
    assert not failures, "\n" + "\n".join(failures)


def test_scenario_ids_are_global_and_active_index_is_exact() -> None:
    active = sorted(ACTIVE_ACCEPTANCE.glob("S*.md"))
    archived = sorted(ARCHIVED_ACCEPTANCE.glob("S*.md"))
    seen: dict[str, Path] = {}
    for path in active + archived:
        metadata = _frontmatter(path)
        for required in ("id", "status", "tier", "driver", "covered_by"):
            assert required in metadata, f"{path} lacks {required}"
        scenario_id = metadata["id"]
        scenario_number = scenario_id.split("-", 1)[0]
        assert path.name.startswith(f"{scenario_number}-"), f"{path} disagrees with {scenario_id}"
        assert scenario_number not in seen, (
            f"{scenario_number} reused by {seen[scenario_number]} and {path}"
        )
        seen[scenario_number] = path

    rows: dict[str, tuple[str, str, str]] = {}
    for line in (ACTIVE_ACCEPTANCE / "README.md").read_text().splitlines():
        match = INDEX_ROW.match(line)
        if match:
            scenario_id, filename, status, driver = match.groups()
            rows[scenario_id] = (filename, status.strip(), driver.strip())

    active_by_id = {_frontmatter(path)["id"].split("-", 1)[0]: path for path in active}
    assert set(rows) == set(active_by_id)
    for scenario_id, path in active_by_id.items():
        filename, status, driver = rows[scenario_id]
        metadata = _frontmatter(path)
        assert filename == path.name
        assert status == metadata["status"]
        assert driver == metadata["driver"]


def test_active_scenario_evidence_paths_exist() -> None:
    missing: list[str] = []
    for scenario in sorted(ACTIVE_ACCEPTANCE.glob("S*.md")):
        for evidence in _frontmatter_list(scenario, "covered_by"):
            for referenced_path in EVIDENCE_PATH.findall(evidence):
                if not (ROOT / referenced_path).is_file():
                    missing.append(f"{scenario.name}: {referenced_path}")
    assert not missing, "missing acceptance evidence:\n" + "\n".join(missing)


def test_current_design_has_no_archived_or_superseded_authority() -> None:
    design_sources = list((DOCS / "specs").glob("*.md"))
    design_text = "\n".join(path.read_text() for path in design_sources)
    assert "archive/" not in design_text

    current_text = "\n".join(path.read_text() for path in _current_markdown())
    assert "research-control-panel-blueprint.md" not in current_text
    assert "docs/design/" not in current_text
    assert "--dangerously-bypass-approvals-and-sandbox" not in current_text
    assert "Work has unrestricted repository" not in current_text
    assert "unrestricted Work permissions" not in current_text
    assert "no graph branches" not in current_text.lower()
    assert "single canonical graph" not in current_text.lower()

    provider_spec = (DOCS / "specs" / "providers-and-containment.md").read_text()
    assert "project repository write roots" in provider_spec
    assert "never the dangerous\nbypass flag" in provider_spec
    assert "They never use `bypassPermissions`" in provider_spec
    assert "complete inventory of every registered project manifest" in provider_spec
    assert "Work-like scope and launch fail closed" in provider_spec

    branch_spec = (DOCS / "specs" / "auto-research-and-branch-merge.md").read_text()
    assert "Every Auto-research episode owns one persistent canonical graph branch" in branch_spec
    assert "human dispatcher" in branch_spec


def test_design_index_links_each_module_spec_once() -> None:
    links = {
        Path(target.partition("#")[0]).name
        for target in _local_links(DOCS / "design.md")
        if target.startswith("specs/") and target.partition("#")[0].endswith(".md")
    }
    assert links == EXPECTED_SPECS


def test_s125_records_implemented_verification() -> None:
    metadata = _frontmatter(ACTIVE_ACCEPTANCE / "S125-auto-research-graph-branch-merge.md")
    assert metadata["status"] == "implemented"
    assert metadata["last_passed"].startswith("2026-08-18")
