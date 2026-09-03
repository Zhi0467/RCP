from __future__ import annotations

from rcp.transport.remote_repository_browser import browse_directory


def test_remote_repository_browser_lists_only_one_bounded_directory_level(tmp_path) -> None:
    repository = tmp_path / "paper"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / ".research").mkdir()
    nested = repository / "nested"
    nested.mkdir()
    (nested / "not-listed").mkdir()
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    (tmp_path / "file.txt").write_text("not a directory", encoding="utf-8")

    listing = browse_directory(str(tmp_path), max_entries=200)

    by_name = {entry["name"]: entry for entry in listing["entries"]}
    assert set(by_name) == {"ordinary", "paper"}
    assert by_name["paper"]["git_repository"] is True
    assert by_name["paper"]["has_research"] is True
    assert "nested" not in by_name
    assert listing["truncated"] is False


def test_remote_repository_browser_caps_directory_iteration(tmp_path) -> None:
    for name in ("one", "two", "three"):
        (tmp_path / name).mkdir()

    listing = browse_directory(str(tmp_path), max_entries=2)

    assert len(listing["entries"]) == 2
    assert listing["truncated"] is True
