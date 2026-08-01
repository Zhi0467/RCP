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
   a small optional graph reflection. Let it finish.
3. Repeat the Work launch with the other installed provider when both Codex and
   Claude are available.

## Assert

- `check provider_launched` — the CLI actually started; no flag was rejected
- `check_work_permissions` — Codex uses automatic non-interactive review and
  Claude uses `acceptEdits`; both can edit the exact run-scope fixture and write
  the optional scratch patch without an RCP approval interaction. Claude's
  `auto` argument is deliberately excluded because its non-interactive mode
  normalizes it to `default` and denies the required writes
- `check_network_enabled` — the launch receipt records network access for Work
  and the provider accepts the setting
- `check patch_came_from_the_file` — read from `patch.json` in the scratch
  folder, never parsed out of the message stream
- `check answer_came_from_the_labelled_final_message` — not "the last text
  emitted", which for Codex is usually a tool or reasoning item
- `check patch_validated` — whatever it wrote passed the schema
- `check whole_graph_entered_the_run` — full graph and canonical `research.md`
- `check only_run_scope_repos_entered_as_pointers` — project scope did not leak
  in as raw content
- `check provider_cwd_was_the_scratch_folder`
- `check_canonical_state_not_a_work_output` — no provider receives a direct
  canonical `.research` write path
- `check graph_matches_log`
- `check no_server_traceback`

Do not assert what the agent said or which nodes it chose. Those change every
run and pinning them makes this useless.

## Failure means

The seam between RCP and a real provider broke — a renamed flag, a changed
output format, a prompt that stopped producing a parseable deliverable. A fake
agent will pass happily through every one of these.
