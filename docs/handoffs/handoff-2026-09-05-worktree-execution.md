# Worktree execution — discussion draft

Status: draft planning PR, not an implementation authorization or current product
contract. RCP already runs unrelated tasks concurrently, but does not manage Git
worktrees. The human confirmed the desired entrance on 2026-09-05; workspace
lifecycle and integration semantics still require discussion before implementation.

## Confirmed direction

- Offer **Work in a worktree** in the conversation composer.
- A task started that way works in a separate Git working directory, allowing
  independent code edits without an exclusive lease on the shared checkout.
- Expose an explicit human **Merge back to main** action for that work.
- This concerns repository files and Git history, not RCP's Auto-research graph
  branch or its separate human-dispatched semantic merge.
- Keep ordinary shared-checkout parallel work available. Do not add a global
  repository lock as a substitute for workspace selection.
- Implementation belongs in its own PR, not the glossary/graph-editing work.

## Existing authority to preserve

Read [providers and containment](../specs/providers-and-containment.md),
[project repository scopes](../specs/projects-spaces-and-operations.md), and
[the graph-only branch boundary](../specs/auto-research-and-branch-merge.md#graph-only-boundary).
Read the current source and tests before selecting an implementation. A graph
branch never isolates repository edits, jobs, or provider sessions.

## Decisions required before implementation

1. Ownership: does the worktree belong to a conversation, an episode, or a single
   task? How do later turns, Pause/Resume/Retry, and restart recovery find exactly
   the same workspace rather than silently creating another?
2. Starting state: named branch/commit, current checkout, or uncommitted changes?
   What does the UI's "main" mean when the repository's default branch differs?
3. Multiple repositories: which repository gets a worktree, and how are other
   registered repositories read or written? Canonical `.research` history must
   keep one owner and must not become an independent copied graph authority.
4. Integration: preview the exact source/destination, handle dirty destinations,
   moving branch heads and conflicts, and require an explicit human action.
   Do not silently reset, overwrite, or force-push. Decide whether integration
   commits, squashes, or uses another explicit Git operation.
5. Lifecycle: retain failed work, expose unmerged changes, define safe cleanup
   and what remains after integration. Task completion is not merge approval.
6. Execution: prove the same ownership, containment and recovery contract locally
   and over SSH, including provider-native session paths and team OS ownership.

## Next step and verification target

Discuss those choices one at a time with the human. Then replace this draft with
a ready execution contract, confirm the durable acceptance journey, and implement
through existing repository-scope and provider owners. Verify two independent
edits, native-session continuation, app restart, SSH disconnect, integration
conflict, rejected integration and cleanup without lost unmerged work. No
production mutation or provider invocation is authorized by this document.

## Suggested skills

Use `grilling` for the unresolved design and `frontend-design` when implementing
the composer and integration controls. This draft should be archived when its
work is completed or rejected, not maintained as a diary.
