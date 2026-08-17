---
id: S96-joining-a-team-space
status: implemented
tier: hermetic
driver: pytest + api + browser
covered_by:
  - tests/test_storage.py::test_team_space_initialization_requires_and_preserves_one_named_bootstrap
  - tests/test_storage.py::test_only_a_team_space_name_is_mutable_while_space_identity_is_not
  - tests/test_main.py::test_space_init_creates_a_named_team_without_locking_or_serving
  - tests/test_main.py::test_space_init_refuses_noninteractive_output_and_an_existing_space
  - tests/test_main.py::test_serve_never_emits_the_team_bootstrap_credential
  - tests/test_team_authentication.py
  - web/tests/api.test.mjs
  - web/tests/teamEnrollment.test.mjs
invariants: [3, 8]
last_checked: 2026-08-15 — an isolated served team space drove steps 1–4. Init
  printed the bootstrap code once and serving printed no credential, the browser
  showed the login page instead of the index, the pasted token left no trace in
  the DOM or JavaScript storage, the session cookie was invisible to script and
  survived reload, the index opened under the member's name, reusing the
  bootstrap code was refused, and an invitation rendered its space name and
  expiry beside a code absent from the URL. Steps 5–11 — a second member,
  invitation reuse and lockout, rotation and revocation against running work,
  and the credential sweep — were not driven.
---

# Join a team space once, and stay joined

This scenario is human-confirmed. It implements the server half of enrollment
and the browser login boundary from
[Team authentication and membership](../design/team-authentication-and-membership.md).
The later desktop **Add team space** form, SSH transport, and operating-system
credential storage are outside this scenario.

A person initializes a named team space deliberately, then claims it with the
one bootstrap code shown by that command. Every later person joins through an
invitation created by an existing member. Each receives one permanent personal
credential, exchanges it once at the team server for a browser session, and is
then attributed as themselves. Restarting the server does not make anyone
enroll again.

Every member has equal product authority. RCP has no administrator rank, and a
member can act only as themselves: they cannot read, rotate, revoke, or use
another member's credential. Machine operations such as backup, restore,
update, and member removal remain outside the product and require
operating-system privilege.

## Setup

A throwaway data directory, one named team space initialized with
`rcp space init --team`, and a personal space used as a control.

## Drive

1. Initialize the team space with a required name. Read the one-time bootstrap
   code printed by `rcp space init --team`, then start the server and confirm
   that serving it prints no credential. Attempt to serve the same team space on
   a non-loopback host while that server is running, and check afterwards that it
   is still serving.
2. Enroll the first named member with that code and receive their permanent
   token. Attempt to use the bootstrap code again.
3. Exchange the permanent token at the browser login boundary. Use the issued
   session to open the project index and perform a member-attributed action.
4. As the first member, create two invitations. Confirm that the invitation
   block names the space and its expiry, and that the member sees only the
   invitations they created.
5. Enroll a second named member with one invitation and receive that member's
   permanent token. Attempt to reuse the invitation, use an expired invitation,
   and guess one invitation repeatedly until that specific code locks.
6. Restart the backend. Make an authenticated request through each member's
   existing browser session and create a fresh session from each permanent
   token.
7. Have the second member attempt to authenticate as, read the token of, rotate
   the credential of, and submit work attributed to the first member.
8. Dispatch work as the second member. Rotate that member's token and confirm
   that the old token and existing sessions stop authenticating while the
   already-authorized work continues and lands under its dispatch-time
   `authorized_by` snapshot. Exchange the replacement token for a new session.
9. Dispatch more work as the second member, revoke their token, and confirm
   that the token and existing sessions stop authenticating while the running
   work remains authorized to finish.
10. Search the SQLite store, server and application logs, prompts, task
    receipts, and canonical history for every raw bootstrap, invitation, and
    permanent token value used during enrollment.
11. Open the personal-space index and use a patch-capable action without any
    login or team-session exchange.

## Assert

- `team_space_init_requires_a_name_and_prints_the_bootstrap_code_once`
- `serve_never_prints_a_bootstrap_code_or_other_credential`
- `a_team_space_refuses_to_serve_on_a_non_loopback_host`
- `a_refused_bind_leaves_the_running_server_untouched`
- `the_first_member_is_created_only_by_the_single_use_bootstrap_code`
- `an_invitation_is_short_lived_single_use_and_visible_only_to_its_creator`
- `the_invitation_block_names_the_space_and_its_expiry`
- `wrong_attempts_lock_only_the_guessed_code_not_an_ip_address`
- `each_member_receives_an_individually_revocable_rcp_prefixed_token`
- `only_an_indexed_sha256_token_hash_is_stored_and_comparison_is_constant_time`
- `the_exchange_endpoint_is_the_only_request_that_receives_a_raw_token`
- `browser_sessions_are_server_side_http_only_secure_same_site_lax_and_slide_for_fourteen_idle_days`
- `the_session_cookie_carries_the_host_prefix_so_it_cannot_be_scoped_to_a_subdomain`
- `restart_preserves_members_tokens_and_sessions_without_re_enrollment`
- `a_member_cannot_authenticate_or_submit_work_as_another_member`
- `a_member_cannot_read_rotate_or_revoke_another_members_credential`
- `rotation_invalidates_the_old_token_and_that_members_existing_sessions`
- `revocation_invalidates_that_members_token_and_existing_sessions`
- `rotation_and_revocation_do_not_stop_already_authorized_work`
- `running_work_lands_under_the_dispatch_time_authorized_by_snapshot`
- `raw_credentials_appear_in_no_store_log_prompt_receipt_or_patch`
- `every_member_has_the_same_product_authority_and_no_admin_role_exists`
- `personal_space_requires_no_login_and_keeps_its_local_owner_identity`

## UI path

Opening a team server without a valid session shows a focused login page rather
than the project index. The person pastes their permanent token into a secret
field and submits it once. A successful exchange clears the token from the page,
sets the server-side session cookie, and opens the team project index under that
member's display name. Reloading and restarting the backend preserve the signed
in state until the sliding idle expiry, logout, rotation, or revocation ends it.

The landing identity panel identifies the current member and allows any member
to create an invitation. The resulting copyable block shows the non-secret space
name and invitation expiry beside the secret code, never places the code in a
URL, and lists only invitations created by that signed-in member. The later
desktop flow that adds and switches among remote spaces is not present here.

A personal space never shows this login page. Its `acting_user` remains the one
durable local owner, and its existing identity naming guard continues to protect
patch-capable actions without introducing a credential.

## Boundary

SSH access and RCP membership stay distinct. This server slice neither creates
an operating-system account nor implements the desktop SSH connection. The
permanent token grants product authority only; backup, restore, update, and
member removal remain console operations under
[S103](S103-server-operations-are-console-operations.md).

Member removal and its stopping policy are not simulated by rotation or
revocation. Those two credential actions invalidate future requests and browser
sessions but deliberately leave already-authorized work alone.

The team backend accepts a raw permanent token only in the exchange endpoint's
request body. The login page holds the pasted value only long enough to make
that request; it is never accepted in a URL or ordinary API request and never
enters JavaScript storage, prompts, receipts, diagnostics, or canonical project
history.

A team space serves only on a loopback host, so a member credential never
crosses plaintext HTTP. Remote members reach it through the encrypted SSH
connection. The design's later direct-HTTPS option therefore needs an explicit
way to say that the connection is already encrypted; until that exists, binding
a team space to a routable address is refused rather than trusted.

This scenario does not decide whether a client should detect that a familiar
`space_id` was rolled back to an older restored archive.
