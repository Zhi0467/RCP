# Worktree execution handoff

Date: 2026-09-05
Status: active, human-confirmed on 2026-09-05; implementation committed on the PR
branch on 2026-09-05. Durable chat binding, local and shipped-remote Git
operations, Work/Discuss path resolution, exact provider write scopes,
continuation and restart recovery, integration preflights and instructions,
explicit removal, UI, regression tests, and four spec updates are implemented. Local HTTP and Git verification proved binding and restart,
isolated fixture edits, dirty refusals, clean preflights, operator-executed local
push and merges, and removal that retains unmerged commits. A later unsandboxed
drive on the committed branch proved the provider path: a real Codex Work turn
created and committed a file on the chat's worktree branch while the shared
checkout stayed untouched, and the human-selected merge-into-starting-branch
turn fast-forwarded the shared checkout onto that commit with both checkouts
clean (receipt below). The Chromium chat-component interaction drive also passes
with fixture API responses. Full served-app/provider browser interaction,
Discuss reads of the worktree, the pull-request option's provider turn, and real Pause/Resume/Retry remain
unverified. A human-approved disposable SSH check on 2026-09-06 verified the
shipped Git module on Git 2.34.1 as the operator account. The full provider/SSH
workflow and GitHub `gh pr create` remain unexercised and blocked on an external
environment. The settled decisions below remain the contract; implementation
choices and check receipts follow.

## What this is

A conversation may do its Work in a separate Git worktree of one registered
repository instead of the shared checkout, so two chats can edit code
independently without an exclusive lease. Integration back is an ordinary
follow-up Work turn that RCP authors from a human choice in the UI. This concerns
repository files and Git history only. It is unrelated to the Auto-research graph
branch and its human-dispatched semantic merge; read the
[graph-only boundary](../specs/auto-research-and-branch-merge.md#graph-only-boundary),
[cooperative write containment](../specs/providers-and-containment.md#cooperative-project-write-containment),
[continuation binding](../specs/providers-and-containment.md#continuation-binding),
and [native chat context](../specs/conversations-episodes-and-watchers.md#native-chat-context)
before touching code.

## Settled decisions

1. **Owner: the conversation.** The worktree is bound to the chat, beside the
   native session and reusable stage the chat already binds. Later turns, Pause,
   Resume, Retry, and restart recovery find the same worktree through that
   durable binding, never through path existence. Episodes and workers do not
   get worktrees.
2. **Composer shape: a tick, not a mode.** The composer offers a **Work in a
   worktree** checkbox while the chat has no Work turn yet. The first Work turn
   sent with it ticked creates and binds the worktree. From then on the chat
   shows a fixed worktree badge and the tick cannot be cleared. Capability is
   unchanged: Work stays Work (invariant 4); only the write target differs.
3. **Starting state.** A new branch created from the commit the shared checkout
   has checked out at binding time. Uncommitted changes in the shared checkout
   are not carried over. The binding records repository alias, machine, worktree
   path, branch name, starting branch name, and starting commit. The UI names
   branches by their real names and never assumes `main`.
4. **One repository in v1.** Binding requires exactly one repository in the
   chat's run scope; otherwise the tick is unavailable and says why. Several
   repositories are a later extension, not a v1 fallback.
5. **Read scope follows the binding.** Every turn of a bound chat, Discuss and
   Work, receives the worktree path as that repository's pointer, so it sees the
   chat's own edits. Read context still grants no write authority (invariant 5).
6. **Write scope admits the worktree.** `ProjectWriteScope` for a bound chat's
   Work turn admits the worktree path as the canonical root for that alias, on
   the same machine and host as the registered root, and does not admit the
   shared checkout. Continuation binding compares the worktree path like any
   other root; a missing or relocated worktree fails before launch. There is no
   fallback to the shared checkout.
7. **Integration is a human-chosen follow-up turn.** A bound chat shows an
   **Integrate** control whose options the backend resolves from the repository:
   **Open a pull request**, **Merge into `<starting branch>`**, and **Merge into
   `<default branch>`** (hidden when it equals the starting branch). Choosing one
   dispatches a Work turn whose instruction RCP authors from the binding facts:
   branch names, starting commit, both paths, and whether the target branch is
   checked out in the shared checkout. When it is not, the agent may check the
   target out inside the worktree to merge and must restore the worktree branch
   afterwards. The agent performs the Git and GitHub operations with the
   execution account's own credentials. RCP holds no token and performs no merge
   itself. Task completion is never integration.
8. **Local-merge turns are the one shared-checkout exception.** For the two
   merge options only, the follow-up turn's write scope admits both the worktree
   and the shared checkout root. Before dispatch RCP preflights on the execution
   machine: the shared checkout has no tracked or untracked changes, the target
   branch exists, and the worktree has no uncommitted changes. A failed preflight
   refuses dispatch and shows the reason. RCP never resets, stashes, commits, or
   force-pushes on the human's behalf. The pull-request option keeps the
   worktree-only scope and runs no destination preflight.
9. **Dirty worktree.** Integration refuses while the worktree has uncommitted
   changes and shows them. The human may send an ordinary Work turn to have the
   agent commit first.
10. **Cleanup is explicit.** Nothing is deleted automatically. A bound chat
    offers **Remove worktree**, which shows the branch's ahead count relative to
    the starting branch and whether a remote branch exists, refuses while the
    worktree is dirty, and otherwise removes the worktree and keeps the branch.
    Failed turns keep the worktree (invariant 9).
11. **Local and SSH.** Creation, preflight, and removal run on the execution
    machine as the execution account through the existing remote command path,
    shipped from a source module. The worktree lives beside the repository root
    at a deterministic path derived from the chat id, outside the shared checkout
    and never under the RCP data directory. Team OS ownership follows the
    existing root checks.

## Owners and file scope

- Binding record and run-scope check: `src/rcp/core/models.py`,
  `src/rcp/conversation_worktrees.py`, and chat binding storage. Existing manifest
  configuration already supplies the needed scope; no configuration change was needed.
- Scope: the `ProjectWriteScope` resolver and continuation binding.
- Git operations: one new module for create, preflight, and remove, used locally
  and shipped remote; never a hand-copied command string.
- Routes: bind on the first ticked Work send, Integrate, Remove.
- Web: composer tick, chat badge, Integrate and Remove controls. Options and
  disabled reasons come from the backend; `web/src/types.ts` restates the shape.
- Specs to update in the same PR: narrow the graph-only boundary's "no Git
  branch, worktree" sentence to the graph branch; add the binding beside native
  chat context; add the worktree root and local-merge exception to write
  containment and continuation binding; add the controls to the projections spec.

## Remaining verification and closure

The complete journey still needs a browser and a provider environment that can
execute the existing containment contract, including the full SSH and GitHub PR
workflows. It must cover: two chats editing the same repository independently;
Discuss in the bound chat seeing the worktree's edits; native session
continuation and app restart finding the same worktree; the same path over SSH;
each Integrate option's preflight refusal and success; a rejected integration and
Remove without losing unmerged work.

The human approved only a disposable SSH Git compatibility check on 2026-09-06;
that does not complete provider execution or GitHub verification. Leave this
file active; do not treat the partial drives or provider task completion as
success.

## Implementation choices where the handoff was silent

- Durable `creating`/`ready`/`removing`/`removed` states make creation and removal
  recoverable. An intact failed removal returns to ready; a removed tombstone
  refuses future turns. An orphan branch without its registered checkout fails
  closed for explicit repair. Project deletion never performs Git cleanup.
- The sibling path and branch use the first 24 hexadecimal digits of SHA-256 of
  the canonical chat UUID. Detached starting HEAD is refused. The default branch
  comes from local `origin/HEAD`; unknown defaults are disabled rather than guessed.
- A binding pins the canonical Git common directory as an exact metadata write
  root, required for branch/index updates from a linked checkout. Ordinary Work
  still excludes shared checkout files. Catalog ownership/overlap validation
  precedes creation and launch, including the metadata directory. Unbound scope
  fingerprints retain their previous serialization.
- Related native turns may transition only between recomputed worktree-only and
  local-merge scopes for the same binding. A bound operation keeps its original
  fingerprint. An interrupted integration may resume on its saved exact target
  branch and restore the bound branch; ordinary turns still require that branch.
- Integration uses the existing task endpoint with a choice enum. The backend
  resolves and persists the target and authors the instruction; injected target
  values are discarded. Integration preserves unsent composer drafts.
- Removal's remote evidence uses an actual `git ls-remote` only when the human
  opens confirmation, never on background polling. Failure is unknown with a
  reason. Ahead count is relative to the current starting branch, or unknown if
  that branch is missing. Git operations use the limits owner's 30-second timeout.
- Executable bindings remain host-local and are excluded from project transfer,
  like native session bindings. Project rekey updates their embedded project id.
  Schema migration 8 and the restore compatibility digest register the new table.
- A local filesystem origin is pushed without invoking GitHub. This makes the
  brief's local-only PR verification boundary explicit in the authored turn.

## Verification receipts (2026-09-05 local date)

Receipts below were captured in the implementing session's disposable scratch
directory, which is not retained; the quoted results are the record. Repository
source baseline was `043aff4`.

### Checks

Fresh setup ran in the prescribed order, each exit 0:
`npm --prefix web ci`, `npm --prefix web exec playwright -- install chromium`,
`npm --prefix web run build`, `uv sync`.

| Command | Exit | Observed result / log under `$SCRATCH` |
| --- | --- | --- |
| `uv run pytest` | 1 | 3,712 passed, 9 skipped, 1 failed; `worktree-pytest-final.log` |
| `uv run pytest tests/test_documentation.py` | 0 | 8 passed after the handoff update; `worktree-documentation-check.log` |
| `uv run ruff check src tests packaging web/src-tauri/scripts` | 0 | All checks passed |
| `uv run pre-commit run --all-files` | 0 | All hooks passed on the final rerun; `worktree-precommit.log` |
| `uv run pre-commit run --files <all 12 untracked new paths>` | 0 | All applicable hooks passed; `worktree-precommit-new-files.log` |
| `npm --prefix web run build` | 0 | TypeScript and Vite passed; existing large-chunk advisory; `worktree-build.log` |
| `npm --prefix web test` | 1 | 605 passed, 3 failed out of 608; `worktree-web-test.log` |

The remaining pytest failure is
`test_pty_runner_supplies_controlling_terminal_for_host_confirmation`:

```text
PermissionError: [Errno 1] Operation not permitted: '/dev/tty'
assert 1 == 0
```

This existing test invokes a local dummy Python prompt, not an SSH host. The
three browser-test failures (wide annotation composer, indexed Experiment reopen,
and the new worktree composer test) all occur before page load:

```text
bootstrap_check_in org.chromium.Chromium.MachPortRendezvousServer.<pid>:
Permission denied (1100)
process did exit: exitCode=null, signal=SIGTRAP
```

The first final formatting pass corrected one extra blank line at the handoff's
EOF (exit 1); no source behavior was changed by that correction. New files were
checked explicitly because `--all-files` sees only tracked paths and the human
prohibited `git add`.

The first full pytest run had 31 failures (3,679 passed, 9 skipped): new schema,
route and request-serialization expectations needed updating,
plus the existing PTY environment failure. The subsequent run had two failures
(3,711 passed, 9 skipped): that PTY failure and a new remote-canonicalization test
fixture missing its service history. The fixture was corrected before the final
run; no production fallback or test skip was introduced.

### Local live drive

The app was served only with `RCP_DATA_DIR=$SCRATCH/worktree-data` and
`uv run rcp serve --host 127.0.0.1 --port 8431`. All servers started for this task
were stopped via their owning terminal session. The final log records orderly
shutdown; a subsequent curl failed to connect (exit 7). Port 8421, its lock, and
the default app data directory were not used.

The registered project `53c70e1d-bb4c-4f26-9f61-243dc72cb970` points to
`$SCRATCH/worktree-fixture/shared`, with local bare origin
`$SCRATCH/worktree-fixture/origin.git`. Starting branch is `topic`, default branch
is `trunk`, and initial commit is `24b92c93c0d259a033dce2cf895d12e7880e6155`.

- GET chooser reported eligible. A first Work request with `worktree=true` was
  accepted (202), creating and binding chat `6f610db4-e31a-46cb-a5e2-897ee70fe184`
  to `shared-rcp-039e537949c521cc092201bd`, branch
  `rcp/chat-039e537949c521cc092201bd`. During implementation the disposable
  binding was supplemented with its independently verified Git common directory
  after that field was introduced; its original JSON is retained in
  `worktree-initial-binding.json`. A subsequent restart on the stable schema
  found the exact unchanged binding (`worktree-binding-before-restart.json`).
- Exactly three real Codex turns were launched: initial Work
  `c8f11d1f-fb3f-45b1-821d-f4d532d7eb82`, Discuss
  `b67638c2-b7b6-4d17-a0fe-7b6780c99fd1`, and independent shared Work
  `5690c657-1dc7-40a6-bc8e-a3c684ae3aee`. Discuss and shared Work were submitted
  concurrently. Discuss reused native session
  `01a0742e-9e59-7072-8934-6cb23c44adbf`; shared Work used another session and its
  own shared-checkout scope. All answers reported
  `sandbox-exec: sandbox_apply: Operation not permitted` before command effects.
  These completed provider turns prove launch/session routing, not successful
  reads, edits, commits, or concurrent editing. Full task receipts are in
  `worktree-live-tasks.json` and `worktree-live-continuations.json`.
- Operator-created fixture files proved checkout independence: `worktree-note.txt`
  appeared only in the worktree and `shared-note.txt` only in the shared checkout.
  With both dirty, all three Integrate POSTs returned 422. After committing only
  the worktree, PR was eligible while both merge POSTs still returned 422. After
  both fixture commits, all three clean preflights were eligible.
- The operator then executed the authored Git recipes, each exit 0: push the
  worktree branch to the local bare origin; merge into checked-out `topic` in
  the shared checkout; switch to `trunk` inside the worktree, merge, and restore
  the worktree branch. The shared checkout stayed on `topic`. These are real
  local Git successes, not provider-driven Integrate successes. No `gh pr create`
  or other GitHub creation was exercised. Commands, outputs, and HTTP responses
  are in `worktree-live-api.json`.
- After an additional worktree commit, removal preview reported ahead 1 and
  actual origin branch presence. Dirty DELETE returned 422 and preserved the
  file. Clean DELETE returned 200, removed the checkout, and retained branch
  commit `eb16335381f3124714b752af17c5249c1c0f6a6d`; `git show` still read
  `unmerged.txt` as `RETAIN_THIS_COMMIT`. A later Work POST returned 422 rather
  than falling back. Receipts are in `worktree-live-removal.json`.
- Server HTTP logs were inspected: expected 422 refusals, no ERROR, traceback,
  HTTP 500, or HTTP 503 matches. Logs are `worktree-server.log`,
  `worktree-server-restart.log`, and `worktree-server-final.log`. Browser console
  and browser network inspection were unavailable: Playwright Chromium launch
  failed under the sandbox and the computer-use tool reported
  `No browser is available`.

### Unsandboxed provider drive (2026-09-05, committed branch)

Served the committed branch on port 8431 against the disposable fixture project.
A new project chat sent a Work turn with `worktree: true` asking Codex to create
`live-check.txt` containing `LIVE_OK` and commit only that file. The task
succeeded: binding `ready` on branch `rcp/chat-9d36d8632b6fc0a211503044` from
starting branch `topic`; the worktree held commit `cc2faf4 Add live check` with a
clean status; the file was absent from the shared checkout, which stayed on
`topic`. A follow-up turn with `worktree_integration: starting_branch` received
the RCP-authored instruction; the agent verified both checkouts, fast-forwarded
`topic` to `cc2faf4`, and both checkouts ended clean with `live-check.txt` present
in the shared checkout. The server log recorded no errors or 5xx responses.

### Explicit gaps

Real-server browser UI interaction/console, Discuss reads of the worktree, the pull-request
option's provider turn, provider-handled merge conflict recovery, and real
Pause/Resume/Retry were not verified. Regression tests exercise those relevant runtime boundaries with fake
provider events and real disposable Git, which does not replace a live provider
drive. The later disposable SSH Git check below does not verify the full provider
workflow as the team service account. GitHub pull-request creation was not
attempted, as instructed; PR verification stops at the local bare-origin push.

### Git 2.34 compatibility check (2026-09-06)

After explicit approval to send the module source to the team host, a disposable
SSH drive under the operator account reproduced the deployed module's
`unknown switch z` failure on Git 2.34.1. The corrected portable porcelain reader
passed plan, create, inspect, merge preflight, and removal with the branch
retained. Both a path with spaces and a path containing Unicode, a tab, quotes,
and a backslash passed. The drive used only temporary repositories, removed them
on exit, and did not touch team projects or the running service. Local regressions
cover both legacy raw paths and modern quoted paths, plus an independent check
of the bound checkout's actual branch.

### PR review verification (2026-09-05)

Review of implementation commit `2643b03` found no unresolved review threads or
known merge-blocking code defect. GitHub CI passed lint/format, pytest on Python
3.11 and 3.12, old-data upgrade, and Web typecheck/tests. A new local run passed
all 86 worktree API/Git/scope and existing write-scope regressions.

The Chromium composer interaction regression also passed outside the restricted
sandbox. It exercises the actual `NodeChat` component with fixture API responses:
first-Work binding selection, branch badge, backend-disabled integration,
ordinary Work integration dispatch without losing the draft, removal evidence,
Cancel, and confirmed removal. No page errors were observed. This closes the
earlier browser-test launch failure, not the real-server/provider/SSH journey.
This handoff remains open for those explicitly listed gaps.
