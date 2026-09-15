"""The one owner of provider account facts: the login rows and the credential store.

The launcher, the sign-in runner, and the skill inventory each need the same
three answers about an account, whether a launch is refused, what state the
account is in once the credential is taken into account, and whether a piece
of provider output means the login died. They share one instance of this class
by constructor injection; nothing wires it into a collaborator after the fact.
"""

from __future__ import annotations

from typing import Literal

from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.providers import profile_for
from rcp.storage import AppStore
from rcp.storage.models import ProviderLoginStateRecord


def account_login_refusal(
    store: AppStore, credentials: ProviderCredentialStore, provider: str, host: str
) -> str | None:
    profile = profile_for(provider)
    if not profile.authentication.credential_available(credentials, host):
        return profile.authentication.missing_credential_detail
    state = store.provider_login_state(provider, host)
    if state.state == "signed_out":
        return f"{profile.label} is signed out. Sign in in Settings, Provider logins."
    return None


def record_provider_failure(
    store: AppStore,
    provider: str,
    host: str,
    *,
    generation: int,
    evidence: str,
    source: Literal["turn", "report", "probe"],
) -> bool:
    """Normalize provider evidence and durably fence only its captured generation."""
    if not profile_for(provider).credential_failure(evidence):
        return False
    store.mark_provider_login_failed(
        provider,
        host,
        generation=generation,
        # Storage folds whitespace and bounds the text; the provider's own words
        # (for example `refresh_token_reused`) stay visible on the Settings card.
        detail=f"Provider authentication failed: {evidence}",
        source=source,
    )
    return True


def reset_logins_without_credentials(
    store: AppStore, credentials: ProviderCredentialStore
) -> list[ProviderLoginStateRecord]:
    reset = []
    for state in store.provider_login_states():
        auth = profile_for(state.provider).authentication
        if state.state == "signed_in" and not auth.credential_available(credentials, state.host):
            reset.append(
                store.mark_provider_login_signed_out(
                    state.provider,
                    state.host,
                    member_id=None,
                    source="restore",
                    detail=auth.missing_credential_detail,
                )
            )
    return reset


class ProviderAccounts:
    """Account policy shared by every component that reads or judges a login."""

    def __init__(self, store: AppStore, credentials: ProviderCredentialStore) -> None:
        self.store = store
        self.credentials = credentials

    @classmethod
    def for_store(cls, store: AppStore) -> ProviderAccounts:
        return cls(store, ProviderCredentialStore.for_data_dir(store.path.parent))

    def refusal(self, provider: str, host: str) -> str | None:
        return account_login_refusal(self.store, self.credentials, provider, host)

    def account_state(self, provider: str, host: str) -> ProviderLoginStateRecord:
        state = self.store.provider_login_state(provider, host)
        auth = profile_for(provider).authentication
        if not auth.credential_available(self.credentials, host):
            return state.model_copy(
                update={"state": "signed_out", "detail": auth.missing_credential_detail}
            )
        return state

    def observe_failure(
        self,
        provider: str,
        host: str,
        *,
        generation: int,
        evidence: str,
        source: Literal["turn", "report", "probe"],
    ) -> bool:
        return record_provider_failure(
            self.store, provider, host, generation=generation, evidence=evidence, source=source
        )

    def reset_logins_without_credentials(self) -> list[ProviderLoginStateRecord]:
        return reset_logins_without_credentials(self.store, self.credentials)
