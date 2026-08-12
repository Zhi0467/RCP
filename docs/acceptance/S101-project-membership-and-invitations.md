---
id: S101-project-membership-and-invitations
status: pending — not human-confirmed
tier: hermetic
driver: pytest + browser
covered_by: none
invariants: [3, 4]
---

# Being in the lab is not being on the project

This scenario is a proposal and is **not yet human-confirmed**. The design is
settled in
[Team authentication and membership](../design/team-authentication-and-membership.md#project-membership).

**Current UI seam (2026-08-12):** S118 reserves team enrollment and
member-invitation controls inside the landing identity panel, but implements no
project invitation records or cards. The project shelf must not manufacture a
sample invitation; Accept/Decline cards arrive only with this scenario's real,
server-derived membership contract.

Space enrollment and project membership are different layers. Joining the lab's
RCP does not admit you to every project in it; a project member invites you, and
you accept.

The invitation appears on the **project index**, not in the Inbox. Inbox is a
destination inside the project shell, reachable only once you are already a
member of that project — so an invitation delivered there could never be seen.
The index is the only surface that exists before membership.

## Setup

A team space with three members. One project created by the first member. The
second and third are space members with no project membership.

## Drive — proposal

1. As the second member, list projects, then attempt to read, dispatch in, and
   apply to that project.
2. As the first member, invite the second member to the project.
3. As the second member, open the project index and find the invitation.
4. Accept it. Open the project, read the graph, and start a task.
5. As the second member, invite the third member. Confirm the second member
   needed no elevated role to do so.
6. As the third member, decline the invitation.
7. As the second member, leave the project, then attempt to read it again.
8. Confirm no token was issued or changed at any point in steps 2–7.

## Assert

- `space_membership_alone_does_not_admit_a_project_read`
- `space_membership_alone_does_not_admit_dispatch_or_apply`
- `a_project_invitation_appears_on_the_project_index`
- `accepting_an_invitation_grants_project_membership`
- `accepting_issues_no_new_token_and_does_not_change_space_membership`
- `every_project_member_may_invite_another_existing_space_member`
- `project_members_have_no_ranks_and_no_owner`
- `an_invitation_cannot_be_sent_to_a_non_member_of_the_space`
- `declining_leaves_no_membership_and_no_residual_access`
- `leaving_a_project_removes_read_dispatch_and_apply`
- `the_server_derives_membership_and_never_reads_it_from_the_request_body`

## UI path (proposal)

Pending project invitations appear on the **project index**, as cards beside the
projects you already have, showing the project name, the space it lives in, and
who invited you. Accepting moves the card into your project list. Declining
removes it.

**Invite to project** lives in the project itself, available to any member, and
offers only people already enrolled in that space.

Deliberately not possible: inviting someone into a project who is not a space
member, an invitation that carries an enrollment secret, and any project role
above "member."

Open for a human answer: whether a space member can *see* that a project exists
before being invited to it, and what happens to a project when its last member
leaves.

## Boundary

A project invitation is an authenticated in-product item addressed to an
existing member identity. It is not the enrollment code from
[S96](S96-joining-a-team-space.md) and cannot be used to join the space.

The index gaining pending items is a real change in what that screen is for — it
becomes a place where things await a response, not only a list of what you have.
That is accepted deliberately, in preference to a second surface also called an
inbox with a different scope.
