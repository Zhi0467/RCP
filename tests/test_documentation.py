from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

EXPECTED_SPECS = {
    "compute-jobs.md",
    "api-web-and-desktop-projections.md",
    "authority-and-proposals.md",
    "auto-research-and-branch-merge.md",
    "conversations-episodes-and-watchers.md",
    "graph-history-and-transitions.md",
    "interface-and-visual-design.md",
    "paper-artifacts-and-result-views.md",
    "projects-spaces-and-operations.md",
    "server-and-machine-operations.md",
    "providers-and-containment.md",
}

EXPECTED_CURRENT_FILES = {
    "docs/decisions/README.md",
    "docs/design.md",
    "docs/handoffs/README.md",
    *(f"docs/specs/{name}" for name in EXPECTED_SPECS),
    "tests/test_documentation.py",
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
    "src/rcp/runs/tasks/branch_merge.py",
    "src/rcp/runs/transition_event_reconciliation.py",
    "web/src/experimentGuidance.ts",
    "web/src/projectTransition.ts",
}

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]*]\(([^)]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


def _current_markdown() -> list[Path]:
    files = [ROOT / "AGENTS.md", ROOT / "README.md"]
    files.extend(DOCS.rglob("*.md"))
    return sorted(files)


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


def test_current_documentation_layout_is_complete() -> None:
    missing_current = [path for path in EXPECTED_CURRENT_FILES if not (ROOT / path).is_file()]
    assert not missing_current, f"missing current documentation: {sorted(missing_current)}"

    assert (DOCS / "design.md").is_file()
    assert not (DOCS / "research-control-panel-blueprint.md").exists()
    assert not (DOCS / "design").exists()
    assert {path.name for path in (DOCS / "specs").glob("*.md")} == EXPECTED_SPECS

    assert not (DOCS / "archive").exists()


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


def test_design_index_links_each_module_spec_once() -> None:
    links = {
        Path(target.partition("#")[0]).name
        for target in _local_links(DOCS / "design.md")
        if target.startswith("specs/") and target.partition("#")[0].endswith(".md")
    }
    assert links == EXPECTED_SPECS
