---
id: S14-remote-state
status: implemented
tier: remote
driver: api
covered_by:
  - tests/test_transport.py::test_confirmed_remote_single_patch_is_success_and_schedules_output_repair
  - tests/test_transport.py::test_failed_remote_single_patch_before_commit_rolls_back_local_mirror
  - tests/test_transport.py::test_unknown_remote_single_patch_is_quarantined_from_local_replay
  - tests/test_transport.py::test_failed_remote_batch_publish_rolls_the_local_mirror_back
  - tests/test_transport.py::test_remote_run_inputs_are_published_as_one_bundle
invariants: [4b, 6, 10b, 10d]
requires: a reachable SSH host with a provider CLI installed
last_passed: 2026-07-30 — all eight checks driven against
  `tianhaowang-gpu0.ucsd.edu`, including the three commit outcomes forced with
  real remote faults. Two defects found and fixed along the way.
---

# Canonical state on another machine

**Driven for the first time on 2026-07-30.** This previously said "cannot run
here, needs a reachable SSH host" until a direct check found one with both
provider CLIs installed and authenticated. All eight checks below passed; the
largest untested surface had been held back by an unchecked assumption rather
than a missing machine.

## Result — 2026-07-30

Setup: a state repo and a code repo at `~/rcp-s14/` on
`tianhaowang-gpu0.ucsd.edu`, canonical state and all graph agents remote.

**Passed**

- `remote_project_opens` — preflight green over SSH, `.research/` created on the
  remote host, project opens and reopens.
- `writes_went_through_the_workspace` — the chat transcript was published into
  remote `.research/chat/` through the workspace; patches appended to the remote
  log. No stray `.batch-*` staging and no held lock after three forced
  interrupts.
- `repository_was_not_copied` — the chat stage was 28K and contained no copy of
  the repository; the agent read it in place from the host and path it was given.
- `conversation_pointers_resolve_on_the_execution_machine` — asked to name the
  files it was given, the agent answered with
  `/home/zhiwang/.codex/sessions/.../rollout-*.jsonl` — the original paths on the
  execution machine, **not** RCP's local `source-cache/` copies — and answered a
  question that could only be answered by having read them.

**Commit-outcome checks — driven over SSH 2026-07-30**

`remote_commit_outcome_classified`, `absent_commit_rolled_back_the_mirror`, and
`unknown_commit_quarantined` were never gaps. The three outcomes are specified
in blueprint D9 and implemented in
[`_reconcile_remote_publish_failure`](../../src/rcp/history/manager.py:550), and
four tests in `tests/test_transport.py` already inject each commit status and
assert the rollback, the quarantine, and the repair flag:

- `test_confirmed_remote_single_patch_is_success_and_schedules_output_repair`
- `test_failed_remote_single_patch_before_commit_rolls_back_local_mirror`
- `test_unknown_remote_single_patch_is_quarantined_from_local_replay`
- `test_failed_remote_batch_publish_rolls_the_local_mirror_back`

What those tests cannot show is that the classification works against a real
remote. That is what this scenario adds, and all three outcomes were forced with
genuine remote faults, driving the real `SSHStateWorkspace`, rsync, and remote
apply script:

| Outcome | Fault | Result |
|---|---|---|
| `absent` | `.research/.publish` made unwritable, which rsync excludes so the local mirror never sees it | remote `mkdir` denied → status `absent` → local commit **rolled back**, nothing quarantined, revision returned to 0 |
| `present` | `graph.json` replaced by a directory the instant the stage appeared, so the apply moves the patch and then fails — twice, since the retry meets the same directory | commit **kept** on both sides, no exception raised, `materialization_repair_required` set, revision advanced to 1 |
| `unknown` | the probe made to fail as a dead host would | commit had landed remotely, local copy **quarantined** as `.unconfirmed-000002.json-…`, repair flagged; the next refresh proved it landed and materialized revision 2 |

`no_duplicate_sync_offered` follows from the same run: two commits produced
exactly `000001.json` and `000002.json`, never a duplicate, and the interrupted
commits left the remote derived files stale rather than inviting a second Sync.
Reopening the project through the app repaired them from the log — remote
`graph.json` came back at revision 2 — which is D9's startup repair.

Only one thing in the three was simulated: the `unknown` probe. Making `test -f`
answer neither yes nor no requires the host to vanish in the window between the
failed apply and the probe, which cannot be scheduled on someone else's machine.
Everything else in that case — workspace, rsync, remote apply, patch log,
quarantine, refresh — was real.

The harness lives outside the repo at `scratchpad/s14_commit.py`; it hardcodes
the host, so it is a record of the run rather than a check anyone can re-run.

**Also confirmed, over the remote path**

- Invariant 9 — a failed run kept its scratch folder and `patch.json` on the
  remote host, with a `patch_retained` receipt.
- A rejected chat patch reported `applied_revision: null` and **the reply
  survived its rejection**. That means no graph change was applied; under the
  v0.5 admission contract, a persisted rejection still occupies an auditable
  log revision.
- [S13](S13-replay-halts.md) — the three invalid patches in this older remote
  fixture carried no recorded rejected-admission receipt, so replay treated
  them as accepted and halted at the first structural failure
  (`replay_status: degraded`, with the revision and rule) rather than making it
  vanish.

**Defects found and fixed**

- A dropped connection reported `bash: cannot set terminal process group (-1):
  Inappropriate ioctl for device` as the failure reason. `bash -lic` writes that
  on every remote run, so it masked the real cause. Now: *"The connection to
  tianhaowang-gpu0.ucsd.edu ended (SIGKILL) before codex finished."*
- An unreachable host was reported as *"codex CLI is not installed on
  murphybox"*, sending the human to install software on a machine they cannot
  log into. ssh's 255 is now distinguished from `command -v`'s 1.

**Reproducible, and worth its own scenario:** four real Codex seeds and chats
against a fresh project produced four patches, all rejected by graph validation.
The first three used evidence that described the confound as an "open blocker",
so steering was a fair suspicion. The fourth deliberately removed that word and
described the same confound as a caveat — and produced the *same two rejections*,
`gated-blocker` and `ungrounded-hypothesis-scope`.

So this is not prompt steering. On a project with no graph yet, the agent
reliably creates a gated blocker directly instead of via a Proposal, and gives a
hypothesis a scope no cited excerpt contains. RCP is right to refuse all four.
But a human seeding a new project hits a multi-minute run that ends in a
rejection, every time, and no scenario covers that today.

## Setup

An SSH host that RCP can reach, with a state repository on it and a provider CLI
installed. A repository on that host, outside the state repo, to be read as run
scope.

## Drive

1. Configure a project whose canonical state is on the remote host.
2. Open it. The graph renders.
3. Refresh. Let it finish.
4. Start a chat turn with **graph changes** on, against a conversation whose
   session file lives on the remote host.
5. Interrupt the connection mid-run, then reconnect.

## Assert

- `check remote_project_opens`
- `check writes_went_through_the_workspace` — lock taken, only changed files
  published; no route handler wrote a canonical file directly
- `check repository_was_not_copied` — the agent was given the host and path, and
  read the repo over SSH
- `check run_inputs_are_transferred_as_one_bundle` — schema, contract, prepared
  context, authorized keys, and projected conversations become visible together
  after one transfer; no half-staged input set can launch
- `check conversation_pointers_resolve_on_the_execution_machine` — no pointer
  names RCP's local cache path; anything unreachable from there is reported as
  unreachable rather than named
- `check remote_commit_outcome_classified` — confirmed, absent, or unknown
- `check absent_commit_rolled_back_the_mirror`
- `check unknown_commit_quarantined` — not replayed, and later canonical work is
  blocked behind repair rather than inviting a duplicate Sync
- `check no_duplicate_sync_offered`

## Failure means

The failure mode here is not a crash — it is a mirror and a canonical repo that
quietly disagree about what happened.
