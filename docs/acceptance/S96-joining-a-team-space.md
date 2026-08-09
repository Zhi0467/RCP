---
id: S96-joining-a-team-space
status: pending — not human-confirmed
tier: hermetic
driver: pytest + api
covered_by: none
invariants: []
---

# Join a team space once, and stay joined

This scenario is a proposal and is **not yet human-confirmed**. The design is
settled in
[Team authentication and membership](../design/team-authentication-and-membership.md).

The first member claims the space with a code the server printed to its own
terminal. Everyone after that is invited by an existing member. Each person ends
up with one permanent personal credential that identifies them individually, and
nobody re-enrolls because the server restarted.

Every member has equal space authority, and equality is preserved by keeping
dangerous operations off the product surface entirely rather than by ranking
people.

## Setup

A freshly initialized team space that has never had a member, served in a
throwaway data directory.

## Drive — proposal

1. Start the server for the first time and read the one-time bootstrap code from
   its terminal output.
2. Enroll the first member with that code. Then attempt to use the same code
   again.
3. As the first member, create an invitation. Enroll a second member with it,
   choosing a member name. Then attempt to reuse that invitation.
4. Attempt to enroll with an expired invitation, and with a wrong code several
   times in a row.
5. Restart the backend. Make an authenticated request as each member.
6. Have the second member attempt an action attributed to the first member.
7. Rotate the second member's token, then use the old one.
8. Revoke the second member's token and attempt to use an existing session.
9. Search the server's logs, task receipts, prompts, and canonical history for
   the token and invitation values.

## Assert

- `the_first_member_is_created_only_by_the_terminal_bootstrap_code`
- `a_bootstrap_code_cannot_be_used_twice`
- `an_invitation_is_single_use_and_short_lived`
- `an_expired_or_wrong_code_is_refused_and_rate_limited`
- `each_member_receives_an_individually_revocable_token`
- `restart_does_not_require_re_enrollment`
- `a_member_cannot_submit_work_attributed_to_another_member`
- `rotation_invalidates_the_previous_token`
- `revocation_invalidates_existing_sessions`
- `tokens_and_invitations_appear_in_no_log_prompt_receipt_or_patch`
- `every_member_has_the_same_space_level_authority`

## UI path (proposal)

**Add team space** on the project index asks for the server's SSH address, the
member's SSH username, and the code, with the code entered through an
interactive secret input rather than an ordinary text field. On success the
space appears in the index with its name and the projects the member can see.

**Invite member** is available to any member and produces a copyable block
containing the non-secret team name and SSH coordinates alongside the secret
code. The secret is never placed in a clickable URL.

Deliberately not possible: enrolling through a link, sharing one lab credential,
and entering a token as a command-line argument.

Open for a human answer: whether the invitation block shows an expiry, and
whether a member can see the list of invitations they created.

## Boundary

SSH access and RCP membership stay distinct. An invitation does not create an
operating-system account, and having an SSH login does not make someone a
member.

Enrollment through the `rcp` CLI is not promised. The server prints its
bootstrap code to its own terminal, and members join through the app; see
[Team API compatibility](../design/team-api-compatibility.md#the-cli-is-not-an-application-client).

Member *removal* is a console operation and belongs to
[S103](S103-server-operations-are-console-operations.md).
