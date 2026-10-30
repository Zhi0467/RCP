"""Provider login facts and the single per-turn login resolver."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.providers import profile_for
from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.models import (
    MACHINE_LOGIN_ID,
    LoginKey,
    ProviderLoginStateRecord,
    _required_timestamp,
)

LoginSwitchReason = Literal[
    "unhealthy", "limited", "sender_setting", "setting_changed", "preferred_source_available"
]
LoginSource = Literal["own", "shared"]


@dataclass(frozen=True)
class LoginRefusal:
    source: LoginSource
    detail: str
    login: LoginKey | None = None
    failure_kind: Literal["provider_auth", "session_refused"] = "provider_auth"


@dataclass(frozen=True)
class PreviousProviderLogin:
    login: LoginKey
    provider_started_at: str
    authorized_by: str | None


@dataclass(frozen=True)
class TurnLoginInputs:
    lineage_login: LoginKey | None
    bound_login: LoginKey | None
    previous: PreviousProviderLogin | None


def account_login_refusal(
    store: AppStore, credentials: ProviderCredentialStore, key: LoginKey
) -> str | None:
    profile = profile_for(key.provider)
    state = store.provider_login_state(key)
    if state.removed_at:
        return "This sign-in was removed. Start a new chat."
    if not profile.authentication.credential_available(
        credentials, key.host, key.login_id, login_state=state
    ):
        return profile.authentication.missing_credential_detail
    if state.state == "signed_out":
        return f"{profile.label} is signed out. Sign in in Settings, Sign-ins."
    return None


def record_provider_failure(
    store: AppStore,
    key: LoginKey,
    *,
    generation: int,
    evidence: str,
    source: Literal["turn", "report", "probe"],
) -> bool:
    """Normalize provider evidence and durably fence only its captured generation."""
    if not profile_for(key.provider).credential_failure(evidence):
        return False
    store.mark_provider_login_failed(
        key,
        generation=generation,
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
        if state.state == "signed_in" and not auth.credential_available(
            credentials, state.host, state.login_id, login_state=state
        ):
            reset.append(
                store.mark_provider_login_signed_out(
                    LoginKey(state.provider, state.host, state.login_id),
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

    def refusal(self, key: LoginKey) -> str | None:
        return account_login_refusal(self.store, self.credentials, key)

    def account_state(self, key: LoginKey) -> ProviderLoginStateRecord:
        state = self.store.provider_login_state(key)
        auth = profile_for(key.provider).authentication
        if not auth.credential_available(
            self.credentials, key.host, key.login_id, login_state=state
        ):
            return state.model_copy(
                update={"state": "signed_out", "detail": auth.missing_credential_detail}
            )
        return state

    def observe_failure(
        self,
        key: LoginKey,
        *,
        generation: int,
        evidence: str,
        source: Literal["turn", "report", "probe"],
    ) -> bool:
        return record_provider_failure(
            self.store, key, generation=generation, evidence=evidence, source=source
        )

    def reset_logins_without_credentials(self) -> list[ProviderLoginStateRecord]:
        return reset_logins_without_credentials(self.store, self.credentials)

    def bound_login(self, provider: str, host: str, native_session_id: str) -> LoginKey | None:
        """Earliest provider-started task owns the session, including fork-source lookups."""
        previous = self._session_login(provider, host, native_session_id, latest=False)
        return previous.login if previous else None

    def previous_login(
        self, provider: str, host: str, native_session_id: str, *, exclude_operation_id: str
    ) -> PreviousProviderLogin | None:
        """Latest provider-started turn, excluding the task about to launch."""
        return self._session_login(
            provider,
            host,
            native_session_id,
            latest=True,
            exclude_operation_id=exclude_operation_id,
        )

    def _session_login(
        self,
        provider: str,
        host: str,
        native_session_id: str,
        *,
        latest: bool,
        exclude_operation_id: str = "",
    ) -> PreviousProviderLogin | None:
        order = "DESC" if latest else "ASC"
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT task.login_id, task.authorized_user_id, MIN(start.created_at) AS started "
                "FROM graph_runs AS task JOIN graph_run_receipts AS start "
                "ON start.operation_id = task.operation_id "
                "AND start.category = 'provider_runtime_selected' "
                "WHERE json_extract(task.request_json, '$.provider') = ? "
                "AND COALESCE(task.stage_host, '') = ? AND task.native_session_id = ? "
                "AND task.operation_id != ? AND task.login_id IS NOT NULL "
                "GROUP BY task.operation_id "
                f"ORDER BY started {order}, task.rowid {order} LIMIT 1",
                (provider, host, native_session_id, exclude_operation_id),
            ).fetchone()
        if row is None:
            return None
        return PreviousProviderLogin(
            LoginKey(provider, host, row["login_id"]), row["started"], row["authorized_user_id"]
        )

    def lineage_login(
        self,
        provider: str | None,
        host: str,
        parent: AgentTaskRecord | None,
        *,
        graph_repair: bool = False,
        lineage_receipt: bool = False,
    ) -> LoginKey | None:
        """Transport reattempts and explicitly recorded graph-repair lineage retain a login."""
        if (
            parent is not None
            and parent.login_id is not None
            and provider is not None
            and provider == parent.request.get("provider")
            and host == (parent.stage_host or "")
            and (
                graph_repair
                or lineage_receipt
                or self.store.agent_task_has_receipt(parent.operation_id, "transport_auto_retry")
            )
        ):
            return LoginKey(provider, host, parent.login_id)
        return None

    def turn_inputs(
        self,
        provider: str,
        host: str,
        *,
        session_id: str | None = None,
        fork_session_id: str | None = None,
        exclude_operation_id: str = "",
        task: AgentTaskRecord | None = None,
        inherit_parent: bool = False,
        lineage_login: LoginKey | None = None,
    ) -> TurnLoginInputs:
        """Capture session facts once; callers explicitly choose whether to inherit lineage."""
        if lineage_login is None and inherit_parent and task and task.parent_operation_id:
            lineage_login = self.lineage_login(
                provider,
                host,
                self.store.agent_task(task.parent_operation_id),
                lineage_receipt=self.store.agent_task_has_receipt(
                    task.operation_id, "provider_login_lineage"
                ),
            )
        bound_session = session_id or fork_session_id
        return TurnLoginInputs(
            lineage_login=lineage_login,
            bound_login=(
                self.bound_login(provider, host, bound_session)
                if bound_session and profile_for(provider).sessions_bound_to_login
                else None
            ),
            previous=(
                self.previous_login(
                    provider, host, session_id, exclude_operation_id=exclude_operation_id
                )
                if session_id
                else None
            ),
        )

    def resolve_launch_login(
        self,
        provider: str,
        host: str,
        member_id: str | None,
        *,
        session_id: str | None = None,
        fork_session_id: str | None = None,
        lineage_login: LoginKey | None = None,
        exclude_operation_id: str = "",
        task: AgentTaskRecord | None = None,
        inherit_parent: bool = False,
    ) -> tuple[LoginKey | LoginRefusal, LoginSwitchReason | None]:
        """Dry-run admission and readiness with the same session and lineage policy."""
        inputs = self.turn_inputs(
            provider,
            host,
            session_id=session_id,
            fork_session_id=fork_session_id,
            lineage_login=lineage_login,
            exclude_operation_id=exclude_operation_id,
            task=task,
            inherit_parent=inherit_parent,
        )
        return self.resolve_login(
            provider,
            host,
            member_id,
            lineage_login=inputs.lineage_login,
            bound_login=inputs.bound_login,
            previous=inputs.previous,
        )

    def resolve_login(
        self,
        provider: str,
        host: str,
        member_id: str | None,
        *,
        lineage_login: LoginKey | None = None,
        bound_login: LoginKey | None = None,
        previous: PreviousProviderLogin | None = None,
        dry_run: bool = True,
        operation_id: str | None = None,
    ) -> tuple[LoginKey | LoginRefusal, LoginSwitchReason | None]:
        """Resolve without writes, or atomically assign and advance the shared LRU clock.

        Callers supply a lineage login only for transport reattempts and the
        initial graph-repair correction. Codex forks supply their source's bound
        login. Human Resume and Retry are ordinary turn boundaries.
        """
        for key in (lineage_login, bound_login, previous.login if previous else None):
            if key is not None and (key.provider, key.host) != (provider, host):
                raise ValueError("Session login does not match the launch provider and host.")
        result: tuple[LoginKey | LoginRefusal, LoginSwitchReason | None] | None = None

        def select(connection: sqlite3.Connection) -> LoginKey | None:
            nonlocal result
            result = self._resolve_login(
                connection,
                provider,
                host,
                member_id,
                lineage_login=lineage_login,
                bound_login=bound_login,
                previous=previous,
            )
            return result[0] if isinstance(result[0], LoginKey) else None

        if dry_run:
            with self.store.connection() as connection:
                select(connection)
        else:
            if operation_id is None:
                raise ValueError("Assigning a login requires a task operation_id.")
            self.store.assign_provider_login(operation_id, select)
        assert result is not None
        return result

    def _resolve_login(
        self,
        connection: sqlite3.Connection,
        provider: str,
        host: str,
        member_id: str | None,
        *,
        lineage_login: LoginKey | None,
        bound_login: LoginKey | None,
        previous: PreviousProviderLogin | None,
    ) -> tuple[LoginKey | LoginRefusal, LoginSwitchReason | None]:
        previous_login = previous.login if previous else None
        machine = LoginKey(provider, host, MACHINE_LOGIN_ID)
        setting = self.store.provider_login_setting(
            member_id or "", provider, host, connection=connection
        )
        sources: tuple[LoginSource, ...] = {
            "own": ("own",),
            "own_then_shared": ("own", "shared"),
            "shared_then_own": ("shared", "own"),
            "shared": ("shared",),
        }[setting.setting]
        auth = profile_for(provider).authentication

        def usable(state: ProviderLoginStateRecord) -> bool:
            return (
                state.state == "signed_in"
                and not state.removed_at
                and auth.credential_available(
                    self.credentials, host, state.login_id, login_state=state
                )
            )

        # A removed parent login can never come back, so it stops pinning its
        # lineage; the task resolves like a new turn (a bound session still refuses).
        if (
            lineage_login is not None
            and self.store.provider_login_state(lineage_login, connection=connection).removed_at
        ):
            lineage_login = None
        # Legacy tasks without an authorizer retain the machine login. OpenCode
        # has no RCP-managed added logins, independent of a member's setting.
        fixed = (
            machine
            if not profile_for(provider).authentication.manages_login or member_id is None
            else (
                lineage_login
                or (bound_login if profile_for(provider).sessions_bound_to_login else None)
            )
        )
        if fixed is not None:
            state = self.store.provider_login_state(fixed, connection=connection)
            if usable(state):
                return fixed, None
            source: LoginSource = "own" if state.member_id is not None else "shared"
            removed_bound_session = state.removed_at is not None
            return LoginRefusal(
                source,
                "This sign-in was removed. Start a new chat."
                if removed_bound_session
                else f"{profile_for(provider).label} sign-in is unavailable. Sign in in Settings, Sign-ins.",
                fixed,
                "session_refused" if removed_bound_session else "provider_auth",
            ), None

        rows = self.store.provider_logins(provider, host, connection=connection)
        assignable = [row for row in rows if not row.draining_since and usable(row)]
        allowed = [
            row
            for row in assignable
            if (
                (row.member_id == member_id and "own" in sources)
                or (row.member_id is None and "shared" in sources)
            )
        ]

        def key_for(row: ProviderLoginStateRecord) -> LoginKey:
            return LoginKey(provider, host, row.login_id)

        def limited(row: ProviderLoginStateRecord) -> bool:
            return self.store.provider_login_limited(
                key_for(row),
                continued=previous_login is not None and key_for(row) != previous_login,
                connection=connection,
            )

        def oldest(row: ProviderLoginStateRecord) -> tuple[str, bool, str, str]:
            clock = self.store.provider_login_assignment(key_for(row), connection=connection)
            return (
                clock.last_assigned_at or "",
                row.login_id != MACHINE_LOGIN_ID,
                row.added_at or "",
                row.login_id,
            )

        previous_row = next((row for row in allowed if key_for(row) == previous_login), None)
        free = [row for row in allowed if not limited(row)]
        if not free and previous_row is not None:
            return key_for(previous_row), None
        candidates = free or allowed
        chosen = None
        for source in sources:
            group = [
                row
                for row in candidates
                if (row.member_id == member_id if source == "own" else row.member_id is None)
            ]
            if group:
                chosen = (
                    previous_row
                    if previous_row in group and not limited(previous_row)
                    else min(group, key=oldest)
                )
                break
        if chosen is None:
            label = "your sign-in" if sources[0] == "own" else "the shared sign-ins"
            return LoginRefusal(
                sources[0], f"No usable login in {label}. Open Settings, Sign-ins."
            ), None
        selected = key_for(chosen)
        if previous_login is None or selected == previous_login:
            return selected, None
        prior = self.store.provider_login_state(previous_login, connection=connection)
        if prior.draining_since or not usable(prior):
            reason: LoginSwitchReason = "unhealthy"
        elif self.store.provider_login_assignment(previous_login, connection=connection).limited_at:
            reason = "limited"
        elif previous is not None and previous.authorized_by != member_id:
            reason = "sender_setting"
        elif (
            setting.changed_at
            and previous is not None
            and previous.provider_started_at
            and (
                _required_timestamp(setting.changed_at)
                > _required_timestamp(previous.provider_started_at)
            )
        ):
            reason = "setting_changed"
        else:
            reason = "preferred_source_available"
        return selected, reason
