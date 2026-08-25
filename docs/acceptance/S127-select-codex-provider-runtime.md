---
id: S127-select-codex-provider-runtime
status: implemented
tier: live
driver: pytest + browser + desktop
covered_by:
  - tests/test_codex_app_server.py
  - tests/test_background.py::test_runtime_is_checkpointed_before_the_provider_session
  - tests/test_api_project_state.py::test_project_settings_persist_agent_defaults_and_repository_reads
  - web/tests/settingsDraft.test.mjs
  - served browser save and reload against disposable project data on 2026-08-25
  - live Codex app-server Discuss, Work, Paper, and Desktop inspection on 2026-08-25
reported_by: GitHub issue 4, confirmed by the human on 2026-08-25
last_passed: 2026-08-25 — Project Settings retained app-server through save and
  reload; real local Codex app-server turns returned labelled answers and usage
  under Discuss, Work, and Paper containment; the resulting persisted thread
  appeared in Codex Desktop
---

# Select the Codex provider runtime per project agent profile

Project Settings lets the researcher choose a provider-owned runtime for every
agent profile. Codex app-server remains behind the same provider-call boundary
as Codex exec and Claude stream JSON, including on an SSH execution machine.

## Drive

1. Open Project Settings and select **Codex app server** for Node chat. Save,
   reload, and confirm the manifest and project snapshot retain `app-server`.
2. Run one Discuss turn and one Work turn from that profile. Confirm both use a
   fresh app-server stdio process while retaining their distinct fixed
   capability and filesystem contracts.
3. Confirm the task records `codex.app-server-stdio.v1` before the prompt is
   delivered, then retains the native thread id, labelled answer, usage, and
   ordinary task result.
4. Resume an existing conversation while app-server fails before `turn/start`.
   Confirm exec resumes the same native thread on the same local or SSH machine,
   no app-server failure is shown to the user, and the task records exec.
5. Fail app-server after the `turn/start` write begins. Confirm RCP fails the
   attempt without invoking exec or replaying the prompt.
6. For a local turn, confirm the provider-created persisted thread appears in
   Codex Desktop.

## Assert

- `profile_runtime_is_provider_owned_and_backward_compatible`
- `project_settings_persist_agent_defaults_and_repository_reads`
- `app_server_runtime_normalizes_one_fresh_local_turn`
- `app_server_runtime_uses_the_existing_ssh_wrapper`
- `app_server_falls_back_to_exec_only_before_prompt_delivery`
- `app_server_does_not_fallback_after_prompt_delivery`
- `runtime_is_checkpointed_before_the_provider_session`

## Boundary

Runtime selection does not change task capability, graph authority, project,
host, native thread id, stage, or write scope. RCP starts one provider process
per turn; it does not keep app-server alive between turns. Codex Desktop owns
task ordering and loading, and RCP does not take over or coordinate its tasks.
