from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rcp import provider_skills
from rcp.agents import ProviderReadiness
from rcp.agents.provider_accounts import ProviderAccounts
from rcp.agents.provider_environment import ProviderCredentialStore, ProviderProcessEnvironment
from rcp.provider_skills import ProviderSkillInventoryManager
from rcp.sources.indexer import ConversationIndexer
from rcp.storage import AppStore
from rcp.storage.models import LoginKey


@pytest.mark.parametrize("removed", [False, True])
def test_indexer_reads_added_codex_home(manifest, tmp_path: Path, removed: bool) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    row = store.add_provider_login("codex", "", added_by="member")
    key = LoginKey("codex", "", row.login_id)
    if removed:
        store.finish_provider_login_removal(key)
    home = ProviderCredentialStore.for_data_dir(tmp_path).account_dir(*key) / "codex-home"
    root = home / "sessions"
    root.mkdir(parents=True)
    source = root / "retained.jsonl"
    source.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "added-session",
                    "cwd": manifest.repository_map["repo-a"].path,
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "id": "answer",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "retained answer"}],
                },
            }
        )
        + "\n"
    )
    indexer = ConversationIndexer(manifest, login_store=store)
    session = next(item for item in indexer.build().sessions if item.session_id == "added-session")
    assert session.path == str(source)
    assert any(record.text == "retained answer" for record in indexer.read_records(session))
    assert str(root) not in manifest.sources.codex_roots


def test_remote_added_roots_use_login_host(manifest, tmp_path: Path) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    row = store.add_provider_login("codex", "worker", added_by="member")
    store.add_provider_login("codex", "other-worker", added_by="member")
    indexer = ConversationIndexer(manifest, login_store=store)
    assert indexer.added_codex_session_roots("worker") == [
        f"~/.rcp/provider-homes/codex/{row.login_id}/sessions"
    ]


@pytest.mark.parametrize("machine_usable", [False, True])
def test_skill_probe_uses_healthy_shared_login_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, machine_usable: bool
) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    accounts = ProviderAccounts.for_store(store)
    own = store.add_provider_login("claude", "", added_by="member", member_id="member")
    shared = store.add_provider_login("claude", "", added_by="member")
    ids = [own.login_id, shared.login_id] + ([""] if machine_usable else [])
    for login_id in ids:
        key = LoginKey("claude", "", login_id)
        accounts.credentials.store_token(
            "claude", "", "test-token", member_id="member", now=store.now(), login_id=login_id
        )
        store.mark_provider_login_verified(key, member_id="member", detail="verified")
    observed: list[LoginKey] = []

    def environment(key: LoginKey) -> ProviderProcessEnvironment:
        observed.append(key)
        return ProviderProcessEnvironment()

    manager = ProviderSkillInventoryManager(
        store, accounts=accounts, process_environment=environment
    )
    monkeypatch.setattr(
        provider_skills,
        "_run_process_group",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, json.dumps({"type": "system", "subtype": "init", "skills": ["review"]}), ""
        ),
    )
    result = manager.refresh(
        "claude",
        "",
        "claude",
        ProviderReadiness(
            provider="claude",
            installed=True,
            authenticated=machine_usable,
            binary_path="claude",
            path_state="resolved",
            version="1.0",
        ),
    )
    assert result.status == "fresh"
    assert [skill.name for skill in result.skills] == ["review"]
    assert observed == [LoginKey("claude", "", "" if machine_usable else shared.login_id)]


@pytest.mark.parametrize("block", ["own_only", "draining", "executable", "version"])
def test_skill_refresh_requires_shared_login_and_executable_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: str
) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    accounts = ProviderAccounts.for_store(store)
    row = store.add_provider_login(
        "claude", "", added_by="member", member_id="member" if block == "own_only" else None
    )
    key = LoginKey("claude", "", row.login_id)
    accounts.credentials.store_token(
        "claude", "", "test-token", member_id="member", now=store.now(), login_id=row.login_id
    )
    store.mark_provider_login_verified(key, member_id="member", detail="verified")
    if block == "draining":
        store.set_provider_login_draining(key)

    def unexpected_probe(*args, **kwargs):
        pytest.fail("A blocked skill inventory must not start the provider.")

    monkeypatch.setattr(provider_skills, "_run_process_group", unexpected_probe)
    result = ProviderSkillInventoryManager(store, accounts=accounts).refresh(
        "claude",
        "",
        "claude",
        ProviderReadiness(
            provider="claude",
            installed=block != "executable",
            authenticated=False,
            binary_path="claude",
            path_state="resolved",
            version=None if block == "version" else "1.0",
        ),
    )
    assert result.status == "unavailable"
    assert result.skills == []
