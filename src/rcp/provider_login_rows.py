"""Field-by-field, viewer-scoped provider rows shared by Settings and notices."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from rcp.agents.provider_accounts import ProviderAccounts
from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.providers import profile_for
from rcp.runs import provider_login
from rcp.storage import AppStore
from rcp.storage.models import LoginKey

if TYPE_CHECKING:
    from rcp.runs.provider_sign_in import ProviderSignInRunner


def provider_login_row(
    store: AppStore,
    credentials: ProviderCredentialStore,
    sign_ins: ProviderSignInRunner | None,
    key: LoginKey,
    *,
    viewer_id: str | None,
    machines: list[str] | None = None,
    provider_path: str | None = None,
    agent_used: bool = False,
) -> dict[str, object]:
    state = ProviderAccounts(store, credentials).account_state(key)
    profile = profile_for(key.provider)
    visible = state.member_id is None or state.member_id == viewer_id
    owner = store.space_user(state.member_id) if state.member_id else None
    added_by = store.space_user(state.added_by) if state.added_by else None
    status = (
        sign_ins.running_sign_in(key.provider, key.host, login_id=key.login_id)
        if sign_ins
        else None
    )
    if status and status.started_by != viewer_id:
        status = status.model_copy(
            update={
                "user_code": None,
                "verification_url": None,
                "detail": None,
            }
        )
    metadata = (
        profile.authentication.credential_metadata(credentials, key.host, key.login_id)
        if visible
        else None
    )
    assignment = store.provider_login_assignment(key)
    return {
        "provider": key.provider,
        "host": key.host,
        "login_id": key.login_id,
        "kind": "own" if state.member_id else "shared",
        "owner_user_id": state.member_id,
        "owner_display_name": owner.display_name if owner else None,
        "added_by_display_name": added_by.display_name if added_by else None,
        "added_at": state.added_at,
        "limited_at": assignment.limited_at if store.provider_login_limited(key) else None,
        "draining_since": state.draining_since,
        "bound_running": provider_login.bound_running_count(store, key)
        if key.login_id and state.member_id is None
        else 0,
        "machines": machines or [],
        "state": state.state,
        "generation": state.generation,
        "detail": state.detail if visible else None,
        "source": state.source if visible else None,
        "changed_at": state.changed_at,
        "changed_by": state.changed_by,
        "label": profile.label,
        "sign_in_methods": profile.authentication.methods,
        "managed": profile.authentication.manages_login,
        "login_command": shlex.join(profile.login_command(provider_path or profile.id)),
        "token_instructions": profile.authentication.token_instructions,
        "token": metadata,
        "sign_in": status.model_dump(mode="json") if status else None,
        "provider_path": provider_path,
        "agent_used": agent_used,
    }


def missing_own_login_row(provider: str, host: str, viewer_id: str) -> dict[str, object]:
    """Represent a refused source without inventing a stored login identity."""
    return {
        "provider": provider,
        "host": host,
        "login_id": None,
        "kind": "own",
        "owner_user_id": viewer_id,
        "owner_display_name": None,
        "added_by_display_name": None,
        "added_at": None,
        "limited_at": None,
        "draining_since": None,
        "bound_running": 0,
        "machines": [],
        "state": "signed_out",
        "generation": None,
        "detail": None,
        "source": None,
        "changed_at": None,
        "changed_by": None,
        "label": profile_for(provider).label,
        "sign_in_methods": [],
        "managed": None,
        "login_command": None,
        "token_instructions": None,
        "token": None,
        "sign_in": None,
        "provider_path": None,
        "agent_used": None,
    }
