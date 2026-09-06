---
id: S133-conversation-worktree-execution
status: blocked-external
tier: live
driver: pytest + browser + provider + SSH
covered_by:
  - tests/test_conversation_worktree_git.py
  - tests/test_conversation_worktree_scope.py
  - tests/test_conversation_worktree_api.py
  - web/tests/worktreeChat.browser.test.mjs
last_passed: not yet fully verified; local and bounded SSH Git receipts are in the active handoff
invariants: [4, 5, 9, 10b, 10c]
reported_by: human-confirmed worktree execution brief, 2026-09-05
---

# Conversations edit independently and integrate only by human choice

One conversation can bind a Git worktree of one registered run-scope repository.
It keeps that checkout across Discuss, Work, native continuation, recovery, and
server restart. Other chats may edit the shared checkout concurrently. This is
repository execution, independent of Auto-research graph branches.

## UI path

1. Register a repository with different starting and default branches. Leave an
   uncommitted shared edit, start a chat, tick **Work in a worktree**, and send
   Work. Verify the new branch starts at the captured commit without that edit.
2. While the first chat edits its worktree, send a second chat to edit the shared
   checkout. Verify the paths and files are independent.
3. Send Discuss in the bound native session. Verify it reads the worktree edits
   and cannot write the repository. Send another Work turn, Pause/Resume and Retry
   where appropriate, and restart RCP; verify the exact same worktree binding.
4. Try every Integrate choice with a dirty worktree. Verify refusal names the
   changes and no provider task is admitted. With a clean worktree and dirty
   shared checkout, both local merge choices refuse while pull request remains
   eligible. Missing target branches refuse local integration.
5. Commit through ordinary Work, then exercise all Integrate choices. Verify a
   push and pull request, a merge into the real starting branch, and a merge into
   the distinct default branch with the worktree branch restored. Completion
   alone must not claim integration. Exercise a conflicting merge; the provider
   reports refusal and preserves both branches and their committed work.
6. Remove refuses while dirty or a turn is active/paused. Once clean, verify the
   confirmation's ahead and remote-branch evidence, remove the checkout, and
   prove the branch still retains unmerged commits. Later turns fail clearly.
7. Repeat the same workflow through a reachable SSH execution machine, including
   provider continuation and restart. Git operations use the account on that
   host and the identical shipped source module.

## Failure means

A chat silently switches checkout, Discuss sees the shared edits instead of its
own, an integration bypasses preflight, a request supplies a write root, or
cleanup discards unmerged commits. An episode or worker gaining a repository
worktree also violates this contract.

## Verification boundary

Local verification uses a throwaway checkout and local bare origin. Pushing that
branch does not verify `gh pr create` on GitHub. On 2026-09-06, the human approved
a disposable SSH Git compatibility check: the shipped module's Git lifecycle
passed on Git 2.34.1 under the operator account. The full provider workflow as the
team service account, GitHub PR creation, and the remaining browser journeys are
still unverified, so this scenario remains blocked-external. The active handoff
records exact executed checks and gaps.
