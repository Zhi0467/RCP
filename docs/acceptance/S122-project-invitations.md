---
id: S122-project-invitations
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_project_invitations.py
  - tests/test_team_project_deletion_guard.py
  - web/tests/landingIdentity.test.mjs
  - api 2026-08-15 — a real two-member team space over browser sessions drove
    invite, accept, membership, leaving, and the last-member refusal
  - browser 2026-08-15 — the Members panel in Project Settings rendered its
    member list with Invite and Leave, and the last-member refusal explained
    itself; no server traceback or 5xx
  - browser 2026-08-29 — a disposable one-member team project rendered Leave
    disabled with the add-another-member action and exposed no ordinary Delete
    action; no browser console or application error
last_passed: >-
  2026-08-29 — invitation without credentials, membership acceptance and
  decline, loss-of-membership fencing, token-revocation asymmetry, the exact
  last-member refusal, and the team deletion guard pass their hermetic and
  browser drives.
invariants: [1, 3, 10g]
---

# Someone puts you on the project, and you can leave it

**Confirmed by the human 2026-08-15**, in the grilling session that split
[S101](S101-project-membership.md). S101 makes membership exist and enforces it.
This one is how it **changes**: a member invites you, you accept or decline, and
you can leave.

Everything that can only happen when membership moves lives here — including
what becomes of an agent that was running on your authorization.

## Decided 2026-08-15

- **A project invitation is not an enrollment code.** It is an authenticated
  in-product item addressed to an existing space member, it issues and rotates no
  token, and it cannot be used to join the space. Separate table from the
  space-level invitations in [S96](S96-joining-a-team-space.md).
- **Any member may invite any space member.** No approval chain, no ranks.
- **Invitations appear on the project index**, because Inbox lives inside the
  project shell and is unreachable before membership. The index therefore becomes
  a place where things await a response, not only a list of what you have. That
  is accepted deliberately, over a second surface also called an inbox.
- **Losing membership fences new work the way Stop does.** The turn running now
  finishes normally; no further watcher wake is claimed. This reuses the durable,
  restart-safe fence in invariant 10g rather than adding a mechanism, and it
  never kills a turn mid-flight.
- **The last member cannot leave.** A memberless project would be invisible to
  everyone with no administrator to recover it. Ordinary team-project deletion
  is unavailable because it would orphan the managed checkout and deploy key.
  The only team action named here is to add another project member; personal
  deletion remains separately governed by [S26](S26-delete-project.md).
- **Revoking a token and losing membership are deliberately asymmetric.**
  Revocation is about a credential and does not stop already-authorized work —
  rotating after a lost laptop must not kill a week-long episode. Removal from a
  project is about that project and does fence it. Both scenarios say so out
  loud, so the difference reads as a decision.

## Setup

A team space with three enrolled members. One project created by the first
member, with a live Auto-research episode holding several unspent invocations.

## Drive

1. As the first member, invite the second. Read what the second member sees on
   the project index.
2. Accept it. Open the project, read the graph, and start a task.
3. Confirm no token was issued, rotated, or changed anywhere in steps 1–2.
4. As the second member, invite the third, and confirm no elevated role was
   needed. As the third member, decline it.
5. Attempt to invite someone who is not a member of the space.
6. As the second member, leave the project, then request it again.
7. Start an Auto-research episode as the first member. While a turn is running
   and invocations remain, remove that member from the project. Watch the running
   turn, then watch for the next watcher wake.
8. Retry and Resume that episode's task after the fence.
9. As the sole remaining member, attempt to leave and inspect the exact next
   action after the team-project deletion guard is active.
10. Separately, dispatch work as a member and then revoke that member's token
    while it runs.

## Assert

- `a_project_invitation_appears_on_the_project_index`
- `accepting_an_invitation_grants_project_membership`
- `accepting_issues_no_token_and_does_not_change_space_membership`
- `every_project_member_may_invite_another_space_member`
- `project_members_have_no_ranks_and_no_owner`
- `an_invitation_cannot_be_addressed_to_a_non_member_of_the_space`
- `declining_leaves_no_membership_and_no_residual_access`
- `leaving_removes_read_dispatch_and_apply`
- `losing_membership_lets_the_running_turn_finish_normally`
- `losing_membership_claims_no_further_watcher_wake`
- `a_fenced_episode_cannot_be_resumed_or_retried_back_into_running`
- `the_fence_is_durable_across_a_restart`
- `the_only_member_cannot_leave_the_project`
- `the_team_last_member_refusal_says_to_add_a_member_not_delete_the_project`
- `revoking_a_token_does_not_fence_running_work`
- `the_server_derives_membership_and_never_reads_it_from_the_request_body`
- `no_agent_path_writes_a_membership_row`

## UI path

**On the project index.** A pending invitation is a card beside the projects you
already have, carrying the project name, its space, and who invited you.
Accepting moves it into your list. Declining removes it. The card carries its own
state; there is no explanatory line under it.

**Inside the project.** One **Invite member** control in Project Settings —
placed there because the project shell header is deliberately bare — available to
any member, offering only people already enrolled in that space. It lists them by
name and resolves to the durable user id; duplicate display names are legal. The
same panel lists who is on
the project, with no rank, owner, or role beside any name.

**Leaving** is in the same place. When you are the only member it is visibly
unavailable. The completed team-server target says the project needs another
member and does not offer ordinary team deletion. This is the one case where
the refusal has to explain itself, because the control is otherwise identical.

**Deliberately not possible:** inviting someone who is not in the space, an
invitation that carries an enrollment secret, any project role above member, and
leaving a project alone.

## Boundary

Membership is authority inside RCP, not confidentiality on disk — see
[S101](S101-project-membership.md)'s boundary. Nothing here changes that.

Removing *another* person from a project is not in this scenario. Leaving is your
own act; removal is someone else's, and it belongs with the console operations in
[S103](S103-server-operations-are-console-operations.md) alongside removing a
person from the space entirely.

The revocation drive that [S100](S100-permission-is-checked-twice.md) deferred
lands with this scenario: S100 demonstrates its two gates through graph movement
because no permission could change while a task ran. Membership is that
permission. Add the drive to S100 rather than restating it here, and keep S100
green.
