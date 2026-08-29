---
id: S128-provision-a-team-project-through-desktop-and-server-cli
status: pending
tier: live
driver: pytest + browser + desktop + ssh
covered_by: none
invariants: [3, 4, 6, 8]
---

# A human starts team setup in the app and the server CLI prepares it

This scenario is human-confirmed and pending implementation. It owns the seam
between [Durable project provisioning](../specs/projects-spaces-and-operations.md#durable-project-provisioning)
and the [Confirmed team desktop target](../specs/api-web-and-desktop-projections.md#confirmed-team-desktop-target).

Project setup is one visible wizard with plainly named personal, new-team, and
personal-to-team intents, while machine work stays under server operating-system
authority. The backend owns one durable request; the browser renders it, the
desktop may invoke its fixed command over SSH, and the server CLI performs the
checkout and Git-credential steps and checks the provider authentication already
present on each execution account. None of those parts independently decides
that a project exists.

## Setup

A source-built desktop connected to a source-built team server, one enrolled RCP
member, an empty server-local central-checkout root owned by `rcp`, one reachable
SSH checkout root owned by its configured remote execution account, two GitHub
repositories, and two desktop SSH configurations: one able to invoke the server
CLI directly or through noninteractive `sudo`, and one that grants member
transport only.

## Drive

1. Open the shared project wizard from the personal index, team index, and an
   existing personal project's **Move to team space** action. Confirm the same
   wizard names **Use an existing checkout personally**, **Create a shared team
   project**, and **Move an existing personal project to a team**, with
   unavailable intents omitted or disabled by the personal export answer, team
   import answer, and native relay capability, and each contextual entry
   preselected correctly. Confirm a browser against one backend cannot
   manufacture the cross-space move intent, then
   choose new-team mode. Call the old direct registration API and the ordinary
   setup preflight/create APIs with a path that would be observable if inspected;
   confirm all three are refused without touching that path or the catalog. Supply
   accepted GitHub.com HTTPS and SSH repository forms and intended project
   settings, accept the home-derived SSH central-root default once, then use an
   explicit account-owned mounted root in a second request. Try
   credentials/userinfo, a query or fragment, percent encoding, traversal, a
   local path, `file://`, `ssh://`, an arbitrary host or port, and extra path
   components; confirm each is rejected before request persistence, filesystem
   access, DNS, or another network call.
2. Read the newly persisted request, its **waiting for server setup** status, its
   proposed canonical project id, resolved target paths, and the exact
   `rcp server project provision <request-id>` command. Confirm the paths use the
   proposed project id, then reload the page and restart the backend.
3. From a browser or the member-only SSH connection, attempt to run the machine
   steps. Confirm the UI offers only the copyable operator command.
4. From the desktop with the operator route, click **Run setup now**. Inspect the
   exact SSH argv, the complete numbered plan in both interactive and structured
   output, and the responsibility, typed target, purpose, state, and expected
   success for every step. Confirm a machine step names its host and OS account,
   while an external step names its service, resource, destination URL, and
   required role without inventing a user identity. Confirm the wizard renders
   those structured steps without inventing or omitting one. Interrupt the SSH
   connection once and reopen the request.
5. Let the CLI create a separate repository-scoped deploy key for each GitHub
   repository without any GitHub user login on the server. Confirm its
   key-generation step names the exact machine and execution account. Confirm
   the separate GitHub action in both interactive and machine-readable output
   names `github.com`, the canonical repository, repository settings
   destination, required repository-administrator role, deterministic label,
   public key, **Allow write access** action, expected verification, and same
   command to resume—without claiming to know the administrator's GitHub login.
   Add the key as a repository administrator, deliberately leave write access
   off once, then enable it and retry. Inspect the server-local key root and the
   remote account's verified home-derived key root; search the transport and
   server temporary files for the remote private key.
6. Let the CLI clone or fetch both central checkouts, perform request-scoped Git
   push/readback/cleanup with each key, and check the configured local or remote
   provider execution account without logging it into the provider.
7. Read **operator action needed** for the refused write key and any provider
   failure, then **ready for review** after both are corrected. Compare another
   member's view of the same request.
8. Inspect the final review: repository source, central absolute paths, Git write
   readiness, provider/machine readiness, and the human authorizer. Confirm.
9. Open the new team project, run one task through the chosen provider profile,
   and inspect the checkout owner, task authorization, launch host/account, and
   Git remote.
10. Return to the project index and inspect the new card's actions, then attempt
    the ordinary project-delete API directly. Re-read the project, central
    checkouts, deploy-key fingerprints and paths, and app records.
11. Cancel a second request before confirmation and inspect its prepared files
    and credentials through the CLI's explicit cleanup/reuse disposition. If a
    deploy key was already added to GitHub, leave it once and confirm the request
    remains **operator action needed** with its exact label/fingerprint until
    revocation or explicit reuse is confirmed.
12. Point a third request at an empty repository and read the exact operator
    action needed to push a local-only codebase through the human's ordinary Git
    workflow, plus the exact provisioning command to resume; confirm RCP creates
    no GitHub repository, takes no user token, uploads no member checkout, and
    creates no hidden initialization commit.
13. Point a `create_team_project` request at a repository containing retained
    canonical identity/Patches, then make retained history appear after an
    initially clean preparation. Confirm both preparation and final review stop
    without adopting, archiving, overwriting, or assigning the proposed id; a
    personal identity names **Move to team space** as the correct path.

## Assert

- `the_ui_creates_one_durable_provisioning_request_before_machine_work`
- `personal_team_and_move_are_three_intents_in_one_visible_project_wizard`
- `contextual_entries_preselect_their_intent_without_creating_another_wizard`
- `desktop_composes_authenticated_intent_controls_but_browser_cannot_invent_move`
- `the_backend_exports_the_project_creation_control_for_the_current_space`
- `move_requires_personal_export_team_import_and_native_relay_answers`
- `the_team_index_and_direct_new_project_link_both_open_provisioning`
- `ordinary_team_project_entry_apis_are_refused_before_path_or_catalog_access`
- `personal_direct_project_setup_remains_available`
- `a_new_request_reserves_one_project_id_without_creating_a_project`
- `central_checkout_paths_use_the_reserved_project_id_before_confirmation`
- `ssh_checkout_root_defaults_from_the_verified_remote_home_without_assuming_home_user`
- `an_explicit_remote_central_root_is_durable_reviewed_and_operator_validated`
- `the_cli_cannot_override_the_request_with_an_ad_hoc_checkout_path`
- `request_status_and_next_action_survive_reload_restart_and_an_ssh_drop`
- `the_backend_exports_waiting_in_progress_operator_action_review_completed_and_cancelled`
- `the_browser_renders_backend_status_and_never_infers_it_from_process_output`
- `a_member_token_or_request_id_alone_cannot_perform_machine_steps`
- `the_desktop_invokes_only_the_fixed_cli_with_a_validated_request_id`
- `the_desktop_uses_system_ssh_and_never_collects_a_private_key_or_sudo_password`
- `a_member_only_connection_gets_a_copyable_command_not_a_false_run_button`
- `the_cli_uses_the_running_servers_private_control_socket_instead_of_opening_sqlite`
- `the_deploy_key_prompt_explicitly_requires_github_write_access`
- `the_deploy_key_is_repository_identity_without_a_github_user_login`
- `github_sources_are_canonicalized_before_persistence_or_side_effects`
- `local_arbitrary_host_credential_and_ambiguous_git_sources_are_rejected`
- `interactive_and_structured_operator_actions_name_account_steps_success_and_resume`
- `interactive_and_structured_output_share_one_complete_numbered_step_plan`
- `every_step_names_responsibility_typed_target_purpose_state_and_success_signal`
- `external_steps_require_a_role_without_inventing_a_user_identity`
- `the_wizard_renders_structured_actions_without_parsing_cli_prose`
- `each_github_repository_uses_a_distinct_deploy_key`
- `server_local_and_remote_keys_stay_on_the_accounts_that_own_their_checkouts`
- `remote_home_is_resolved_and_verified_without_trusting_shell_home_or_manifest_input`
- `a_remote_private_key_never_crosses_stdout_progress_or_the_server_filesystem`
- `read_only_git_access_is_operator_action_needed_not_ready_for_review`
- `an_empty_repository_requires_an_operator_created_first_commit`
- `local_only_code_is_pushed_by_the_human_not_uploaded_or_adopted_by_rcp`
- `direct_team_creation_refuses_retained_rcp_identity_or_patch_history`
- `retained_history_appearing_after_preparation_is_rechecked_before_creation`
- `a_personal_canonical_identity_routes_to_transfer_not_adoption_or_overwrite`
- `the_private_deploy_key_never_enters_sqlite_manifest_logs_prompts_or_backup`
- `each_central_checkout_is_owned_by_its_declared_local_or_remote_execution_account`
- `member_checkouts_are_not_discovered_or_imported`
- `git_write_and_provider_execution_are_verified_on_the_declared_accounts`
- `provider_readiness_uses_existing_native_auth_and_never_stores_or_changes_it`
- `machine_preparation_alone_never_registers_the_project`
- `only_final_human_review_appends_the_reserved_project_id_and_canonical_home`
- `the_backend_marks_ordinary_delete_unavailable_for_a_team_project`
- `the_web_omits_delete_from_a_team_card_without_deriving_that_decision`
- `a_direct_delete_request_is_refused_before_records_keys_or_checkouts_change`
- `interactive_and_structured_cli_modes_publish_the_same_durable_progress`
- `cancelled_preparation_has_one_explicit_safe_disposition`
- `cancellation_never_calls_private_key_deletion_github_grant_revocation`

## UI path

The app shows one project wizard, not separate personal, provisioning, and
transfer wizards. Product eligibility and fields come from the applicable
backend; only cross-space move additionally intersects the desktop-native relay
capability and authenticated target list. Shared project, repository, provider,
progress, and review presentation stays in one flow. **Run setup now** is a
desktop-only convenience after an operator probe; **Copy server command** is
always available. Progress steps are compact and resume from durable state
rather than becoming a terminal transcript.

At **operator action needed**, the card renders the CLI's structured
responsibility, typed machine or external-service target, ordered action, safe
command or GitHub destination, nonsecret value, expected success, and resume
command—for example, a repository administrator adds the displayed public key
to the named repository and enables GitHub's **Allow write access**. Secrets are
never echoed. At **ready for review**, the human sees the resolved result and one
**Create project** action.

## Boundary

This scenario provisions a new team project. Personal-to-team transfer reuses
the same target checkout, Git, provider, desktop, CLI, and durable-request
mechanics, then adds the source-space fence and transfer envelope in
[S98](S98-move-a-project-into-a-team-space.md).

The CLI may leave a prepared checkout or deploy key after cancellation only
under an explicit backend-recorded reuse or cleanup disposition. It must not
guess that an untracked directory is safe to delete. Project creation remains a
human product action; server privilege proves the machine can prepare it, not
that the human approved it.
