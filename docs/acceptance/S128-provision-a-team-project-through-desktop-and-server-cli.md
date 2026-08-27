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

Project setup is visible and human-directed in RCP, while machine work stays
under server operating-system authority. The backend owns one durable request;
the browser renders it, the desktop may invoke its fixed command over SSH, and
the server CLI performs the checkout and credential steps. None of those parts
independently decides that a project exists.

## Setup

A source-built desktop connected to a source-built team server, one enrolled RCP
member, an empty central-checkout root owned by `rcp`, two GitHub repositories,
and two desktop SSH configurations: one able to invoke the server CLI directly
or through noninteractive `sudo`, and one that grants member transport only.

## Drive

1. In the team project index choose **Create team project**, supply the repository
   source and intended project settings, and continue.
2. Read the newly persisted request, its **waiting for server setup** status, its
   resolved target paths, and the exact `rcp server project provision
   <request-id>` command. Reload the page and restart the backend.
3. From a browser or the member-only SSH connection, attempt to run the machine
   steps. Confirm the UI offers only the copyable operator command.
4. From the desktop with the operator route, click **Run setup now**. Inspect the
   exact SSH argv and structured progress; interrupt the SSH connection once and
   reopen the request.
5. Let the CLI create a separate repository-scoped deploy key for each GitHub
   repository. Follow its prompt, deliberately leave **Allow write access** off
   once, then enable it and retry.
6. Let the CLI clone or fetch both central checkouts, perform request-scoped Git
   push/readback/cleanup with each key, and verify the configured local or remote
   provider execution account.
7. Read **operator action needed** for the refused write key and any provider
   failure, then **ready for review** after both are corrected. Compare another
   member's view of the same request.
8. Inspect the final review: repository source, central absolute paths, Git write
   readiness, provider/machine readiness, and the human authorizer. Confirm.
9. Open the new team project, run one task through the chosen provider profile,
   and inspect the checkout owner, task authorization, launch host/account, and
   Git remote.
10. Cancel a second request before confirmation and inspect its prepared files
    and credentials through the CLI's explicit cleanup/reuse disposition.

## Assert

- `the_ui_creates_one_durable_provisioning_request_before_machine_work`
- `request_status_and_next_action_survive_reload_restart_and_an_ssh_drop`
- `the_backend_exports_waiting_in_progress_operator_action_review_completed_and_cancelled`
- `the_browser_renders_backend_status_and_never_infers_it_from_process_output`
- `a_member_token_or_request_id_alone_cannot_perform_machine_steps`
- `the_desktop_invokes_only_the_fixed_cli_with_a_validated_request_id`
- `the_desktop_uses_system_ssh_and_never_collects_a_private_key_or_sudo_password`
- `a_member_only_connection_gets_a_copyable_command_not_a_false_run_button`
- `the_cli_uses_the_running_servers_private_control_socket_instead_of_opening_sqlite`
- `the_deploy_key_prompt_explicitly_requires_github_write_access`
- `each_github_repository_uses_a_distinct_deploy_key`
- `read_only_git_access_is_operator_action_needed_not_ready_for_review`
- `the_private_deploy_key_never_enters_sqlite_manifest_logs_prompts_or_backup`
- `each_central_checkout_is_owned_by_its_declared_local_or_remote_execution_account`
- `member_checkouts_are_not_discovered_or_imported`
- `git_write_and_provider_execution_are_verified_on_the_declared_accounts`
- `machine_preparation_alone_never_registers_the_project`
- `only_final_human_review_creates_the_canonical_project_home`
- `interactive_and_structured_cli_modes_publish_the_same_durable_progress`
- `cancelled_preparation_has_one_explicit_safe_disposition`

## UI path

The app shows one setup card whose primary state and action come from the
backend. **Run setup now** is a desktop-only convenience after an operator probe;
**Copy server command** is always available. Progress steps are compact and
resume from durable state rather than becoming a terminal transcript.

At **operator action needed**, the card shows the exact failed check and one
next step—for example, add the displayed public key to the named repository and
enable GitHub's **Allow write access**. Secrets are never echoed. At **ready for
review**, the human sees the resolved result and one **Create project** action.

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
