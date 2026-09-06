# Worktree execution handoff

Date: 2026-09-05
Status: active, human-confirmed on 2026-09-05. Nothing is implemented. The
decisions below were settled in discussion on 2026-09-05 and are repeated here so
this file stands alone. Implementation is authorized through the existing
repository-scope, chat-binding, and provider owners named below, in its own PR,
separate from glossary and graph-editing work.

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

- Binding record and run-scope check: `src/rcp/config.py`, `src/rcp/core/models.py`,
  chat binding storage.
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

## Acceptance

This is a new durable cross-module promise, so it needs one acceptance scenario
at the next free number. It must cover: two chats editing the same repository
independently; Discuss in the bound chat seeing the worktree's edits; native
session continuation and app restart finding the same worktree; the same path
over SSH; each Integrate option's preflight refusal and success; a rejected
integration and Remove without losing unmerged work.

## Closure condition, all of it

1. The acceptance scenario is confirmed and passing, and the specs above
   describe the shipped behavior.
2. All three Integrate options were exercised on a real repository locally and
   over SSH, with receipts recorded in this file.
3. This handoff is archived in the same PR that completes item 2.
