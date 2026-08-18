---
id: S89-provider-native-skill-inventory
status: implemented
tier: live-provider
driver: pytest + browser + ssh
covered_by:
  - tests/test_provider_skills.py
  - tests/test_api.py::test_startup_marks_all_skill_targets_then_refreshes_each_once
  - tests/test_api.py::test_project_snapshot_and_resolution_use_last_good_provider_skills
  - tests/test_prompts.py::test_provider_native_invocation_is_structured_and_cannot_widen_authority
  - tests/test_chat_prompt_protocol.py::test_fresh_discuss_passes_provider_native_receipt_beside_unchanged_message
  - web/tests/skillPicker.test.mjs
  - web/tests/providerSkillReadiness.test.mjs
  - web/tests/runDialog.test.mjs
last_passed: 2026-08-08 — pytest, browser, and live SSH
invariants: [4, 8, 10, 10d]
reported_by: human, 2026-08-08
---

# Refresh provider-native skills once and offer them beside RCP packages

RCP keeps its source-versioned official packages and each configured CLI's own
skill inventory as separate sources. Provider-native inventories are refreshed
once per app startup, after the existing provider path and readiness check, and
are then offered for the matching provider and execution machine in chat and
paper slash menus.

## UI path

1. Start RCP with local and SSH execution machines configured. The app becomes
   healthy and interactive before provider checks finish.
2. Open a project and type `/` in project chat, node chat, or Paper. The menu
   renders **RCP Official Workflows** and **RCP Official Skills** first. It then
   renders a provider group labelled with the selected provider and execution
   machine, such as **Codex Skills · laptop** or **Claude Skills · gpu**.
   Clicking an entry or choosing it with Enter or Tab completes its slash token
   into the composer and leaves the user ready to continue the message.
3. Change the composer provider or execution machine. Only the matching
   provider-native inventory changes; the RCP official groups remain.
4. Select a provider-native skill and send the turn. The visible human message
   contains the completed slash token. The immutable request and provider
   contract record the exact provider, machine, provider version, inventory
   hash, and native skill name selected for that turn.
5. Restart after making one configured SSH host unreachable. Skills from that
   target's last successful refresh remain rendered and selectable, visibly
   stale with the current diagnostic. A target with no successful inventory
   offers no native skills.

## Startup and storage contract

- Startup schedules every known provider target after health is available. For
  an SSH target, RCP first resolves the CLI path and completes the existing
  version, authentication, and catalog readiness path through the shared SSH
  runner. Only a resolved, responding provider starts its skill command.
- Provider profiles own their skill refresh command and parser beside their
  other CLI-specific facts. RCP records the exact command with the resulting
  inventory in app SQLite; neither the manifest nor `.research` stores it.
- A successful refresh atomically replaces that target's last successful
  inventory. A failed path check, command, timeout, or parse marks the prior
  inventory stale and records the diagnostic without replacing its skills,
  successful provider version, command, or inventory hash.
- Refresh runs once in the startup warmup. Project open, navigation, provider
  readiness refresh, and agent launch do not start another skill refresh.
- SSH probes use the existing batch-mode, login-shell, multiplexed SSH path.
  They run off the event loop: first paint and navigation remain responsive,
  while provider groups may show loading until their startup result is known.
- Selecting a provider-native skill does not change `permissions_for()`, CLI
  launch flags, graph authority, repository authority, or the provider's
  captured surface capability. A stale skill that the provider later rejects
  fails visibly and never falls back to an RCP package or another native skill.

## Assert

- `provider_profiles_own_refresh_commands_and_parsers`
- `skill_refresh_follows_resolved_provider_readiness`
- `startup_refresh_runs_once_per_provider_machine_target`
- `project_open_readiness_refresh_and_launch_do_not_refresh_skills`
- `failed_refresh_preserves_last_successful_inventory_as_stale`
- `first_failure_offers_no_native_skills`
- `official_registry_remains_separate_and_first_in_the_menu`
- `click_enter_and_tab_complete_the_selected_slash_token`
- `provider_and_machine_switch_the_native_group`
- `native_invocation_is_structured_and_preserves_the_human_message`
- `native_skills_never_widen_surface_authority`
- `startup_and_remote_probes_do_not_freeze_the_app`
- `live_ssh_inventory_matches_the_configured_remote_cli`

## Failure means

Project open or a slash interaction launches a provider probe, a failed refresh
erases useful last-known skills, stale data is presented as fresh, provider
skills appear under the wrong machine, an official package is mistaken for a
provider-native skill, a selected skill widens permissions, SSH work blocks the
UI, or RCP invents a second provider-specific command registry.
