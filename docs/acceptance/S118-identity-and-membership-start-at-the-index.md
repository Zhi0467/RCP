---
id: S118-identity-and-membership-start-at-the-index
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_identity_api.py
  - tests/test_storage.py::test_space_and_user_identity_fields_are_immutable
  - web/tests/landingIdentity.test.mjs
  - web/tests/api.test.mjs
  - web/tests/attentionRunsOntology.test.mjs::Project Settings supports legacy profiles without an ontology authoring surface
  - browser 2026-08-12 — unnamed Sign in, save and reload, full-id Copy,
    Escape focus return, rename with stable id, disabled team seam, and rendered
    Settings without an identity editor; clean browser diagnostics and no server
    traceback or 5xx
last_passed: 2026-08-12 — the isolated personal-space drive named and renamed
  one durable identity without changing its full id, kept all three team actions
  explicitly disabled, and rendered the project index and Settings cleanly
invariants: [3]
---

# Identity and membership start at the project index

This scenario is human-confirmed. It relocates the implemented personal
identity UI from S112 and reserves one coherent entry point for the future team
enrollment and invitation contracts in S96 and S101. The visible team controls
are an explicit, nonfunctional seam; this scenario does not authorize or
simulate the still-pending team authentication backend.

The project index is the one surface available before a project is opened and
before a person belongs to a team project. Identity and membership therefore
live in one compact control at the upper-right of that index, not in any
project's Settings.

For a personal space, **Sign in** means choosing the display name of the one
durable local RCP identity. It is not a password prompt and never guesses the
operating-system username. A team space still authenticates each member with
the individual credential established during enrollment. The personal space and
each team space assign their own durable user id. The identity card shows both
the current display name and the exact id assigned by the current space.

## Setup

An unnamed personal identity and the same identity after it has chosen a name.

## Drive

1. Open the project index as the unnamed personal identity.
2. Use **Sign in** in the upper-right corner, choose a display name, and reload
   the app. Reopen the identity card and read and copy both the display name and
   full user id.
3. Open the same control under the saved display name, rename the identity, and
   verify that its durable user id did not change.
4. Open a project's Settings and inspect the whole page.
5. Inspect **Join team space**, **Accept invitation**, and **Invite member** in
   the landing identity control. Confirm that each is visibly unavailable and
   that the panel says team connections are not implemented in this build.
   Attempting to use the seam sends no request and stores no credential or
   invitation data.
6. Start a patch-capable action while the personal identity is unnamed. The
   same identity dialog protects the unchanged draft or run form; cancel returns
   without loss, and saving continues the original action once.

## Assert

- `the_landing_index_has_no_choose_a_project_title`
- `identity_is_the_rightmost_control_in_the_landing_header`
- `personal_sign_in_chooses_a_name_without_inventing_a_password_account`
- `the_identity_card_shows_the_name_and_exact_space_scoped_user_id`
- `the_user_id_is_copyable_but_not_editable`
- `the_saved_display_name_replaces_sign_in_without_changing_user_id`
- `project_settings_contains_no_identity_editor`
- `the_identity_control_is_the_single_home_for_renaming_and_space_membership`
- `team_enrollment_and_invitation_actions_are_visible_as_one_explicit_seam`
- `the_team_seam_is_disabled_and_named_as_not_connected`
- `the_team_seam_sends_no_request_and_stores_no_secret`
- `the_write_time_identity_guard_preserves_the_pending_human_action`
- `identity_and_membership_controls_remain_keyboard_and_mobile_reachable`

## UI path

The landing header keeps the RCP mark on the left and open-project tabs in the
middle. Its right edge holds one quiet identity button: **Sign in** before a
personal name is chosen, then the display name and current-space state after
sign-in. Opening it reveals a compact anchored panel rather than a new page.
The first card in that panel labels and shows the display name and the complete
space-scoped user id. The id uses a compact monospace treatment, remains
selectable, and has a dedicated copy action; it is never an editable field.

The panel owns personal naming and renaming. It also shows disabled **Join team
space**, **Accept invitation**, and **Invite member** controls below a persistent
statement that team connections are not implemented. Those controls expose no
credential fields, manufacture no invitation data, and perform no request. S96
will replace this seam with enrollment and member-invitation behavior; S101 will
later add separate project-invitation cards to the project shelf.

The `Choose a project` heading is removed. Project covers remain the first
content on the page and the Experiment board remains below them. No identity
editor remains in Project Settings, and no second account/settings page is
introduced.

The future invitation split remains deliberate: team enrollment and member
invitations belong in the upper-right identity panel, while project invitations
will appear as Accept/Decline cards in the project shelf once S101 is approved
and implemented.

## Boundary

This scenario settles placement, vocabulary, continuity, and the honest UI
boundary before team support exists. S96 still owns every team connection,
credential, enrollment, invitation, secure-storage, expiry, replay-resistance,
rate-limit, session, and revocation behavior. S101 still owns every
project-invitation and project-membership behavior. No S118 control may imply
that either contract is active.
