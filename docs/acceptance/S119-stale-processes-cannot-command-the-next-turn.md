---
id: S119-stale-processes-cannot-command-the-next-turn
status: implemented
tier: live
driver: pytest + ssh
covered_by:
  - tests/test_staged_command_client.py
  - tests/test_launcher.py
  - tests/test_auto_research_stream.py
  - tests/test_auto_research_commands.py
  - tests/test_acceptance_agent.py
  - live SSH 2026-08-12
invariants: [4b, 8, 10g]
reported_by: security audit, 2026-08-12
last_passed: 2026-08-12 — detached prior-turn process rejected locally and on
  tianhaowang-gpu0.ucsd.edu while a real Codex tool descendant completed the
  same command
---

# A stale process cannot command the next Auto-research turn

Confirmed by the human on 2026-08-12. RCP protects an Auto-research invocation from
a process left behind by an earlier RCP turn. It does not claim to remain secure
when the execution account itself is hostile.

## Drive

1. Start an Auto-research actor invocation on a local execution machine. From its
   provider process tree, use the staged client to validate a Patch and issue
   a mutating command. Both complete through the ordinary audited command path.
2. Leave a detached client process alive after that invocation ends. Start the
   actor's next invocation on the exact same reusable stage and native provider
   session, but under a fresh provider OS process tree.
3. Let the detached process discover the new client and broker address and try
   a new command. RCP refuses it before a request reaches the dispatcher or
   command ledger. The current invocation can still issue the same command.
4. Repeat the drive through a configured SSH execution machine. The broker runs
   on that execution host and makes the decision from that host's kernel process
   identity; the local control process does not accept a claimed remote pid.
5. Make broker startup or peer-process inspection unavailable. RCP fails the
   invocation before delivering the provider prompt and never falls back to a
   staged file, environment variable, command-line secret, or unauthenticated
   mailbox request.

## Assert

- `campaign_stage_prompt_environment_and_client_argv_contain_no_bearer_secret`
- `current_provider_process_tree_can_use_every_authorized_command`
- `detached_prior_turn_process_is_rejected_before_dispatch_and_audit_start`
- `same_stage_retry_and_resume_bind_a_fresh_os_process_session`
- `mailbox_request_signatures_reveal_no_reusable_command_authority`
- `local_broker_uses_kernel_peer_pid_and_live_provider_ancestry`
- `ssh_broker_authenticates_on_the_execution_host`
- `broker_or_peer_identity_unavailable_fails_before_prompt_delivery`
- `there_is_no_credential_file_or_unauthenticated_fallback`

## Boundary

The protected attacker is a subprocess retained from an earlier provider turn,
including one that deliberately detached into its own process session. The
current provider and its live tool descendants remain legitimate even when an
ordinary tool starts a new OS session.

An arbitrary hostile process running as the same OS user is out of scope. Such
a process may be able to inspect or interfere with other same-user processes;
resisting it requires OS-level isolation rather than another bearer-secret
format. The broker must not be described as providing that stronger boundary.
