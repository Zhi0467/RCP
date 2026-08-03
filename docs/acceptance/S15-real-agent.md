---
id: S15-real-agent
status: implemented
tier: live
driver: api
covered_by: none
invariants: [4, 4b, 5, 11]
---

# One real agent run, end to end

Everything else uses a fake agent. This one uses a real provider once, because
the fake agent by construction cannot catch a broken prompt, a wrong CLI flag,
or a provider that changed its output format.

Costs money and takes minutes. Run before a release, not on every change.
Nondeterministic — so assert **shape**, never wording.

## Setup

A temporary copy of the demo project. A real provider available locally: Codex
CLI or Claude Code.

## Drive

1. Refresh the project with the real provider. Let it finish.
2. Start a **Work** turn. Ask for one harmless repository edit, one command, and
   a small optional semantic graph reflection. Have it run the staged validator
   client before finishing.
3. Repeat the Work launch with the other installed provider when both Codex and
   Claude are available.

## Assert

- `check provider_launched` — the CLI actually started; no flag was rejected
- `check_work_permissions` — Codex uses
  `--dangerously-bypass-approvals-and-sandbox` and Claude uses
  `--permission-mode bypassPermissions`; both have unrestricted repository and
  tooling access without an RCP approval interaction
- `check_network_enabled` — the launch receipt records network access for Work
  and the provider accepts the setting
- `check_validator_client` — the immutable Python client runs from the writable
  Work workspace, receives a response from RCP, and the task records the bounded
  self-check count and result
- `check patch_came_from_the_file` — read from `patch.json` in the scratch
  folder, never parsed out of the message stream
- `check patch_is_semantic_only` — the provider supplies graph meaning while RCP
  adds patch, Proposal, revision, scope, and lifecycle bookkeeping
- `check answer_came_from_the_labelled_final_message` — not "the last text
  emitted", which for Codex is usually a tool or reasoning item
- `check patch_validated` — whatever it wrote passed the live in-process
  semantic validator and was re-prepared and revalidated at Apply
- `check whole_graph_entered_the_run` — full graph and canonical `research.md`
- `check only_run_scope_repos_entered_as_pointers` — project scope did not leak
  in as raw content; those pointers are context, not a Work permission allowlist
- `check provider_cwd_was_the_scratch_folder`
- `check_canonical_state_boundary_is_prompt_only` — the Work contract forbids
  direct canonical `.research` writes and the receipt describes that known
  limitation as prompt-enforced for both providers, never sandbox-enforced
- `check graph_matches_log`
- `check no_server_traceback`

Do not assert what the agent said or which nodes it chose. Those change every
run and pinning them makes this useless.

## Failure means

The seam between RCP and a real provider broke — a renamed bypass flag, a changed
output format, a staged validator client the provider cannot run, or a prompt
that stopped producing a semantic deliverable. A fake agent will pass happily
through every one of these.
