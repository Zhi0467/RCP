---
id: S112-basic-human-identity
status: implemented
tier: hermetic
driver: pytest + api + browser
covered_by:
  - tests/test_storage.py::test_s111_identity_migrates_to_personal_with_one_unnamed_owner
  - tests/test_storage.py::test_explicit_team_space_preprovisions_distinct_members_with_duplicate_names
  - tests/test_storage.py::test_explicit_space_kind_mismatch_fails_without_changing_stored_kind
  - tests/test_storage.py::test_space_and_user_identity_fields_are_immutable
  - tests/test_identity_api.py
  - web/tests/api.test.mjs
  - web/tests/projectHistory.test.mjs
  - browser 2026-08-11 — cancel-preserved Seed form, save-and-retry, Settings rename
last_passed: 2026-08-11 — the isolated personal-space flow retained the exact
  Seed message on cancel, saved an explicit name, retried the original task once,
  and renamed the durable identity in Settings with clean browser and server logs
invariants: [3, 6]
---

# A person has one durable identity inside a space

This narrow identity scenario was confirmed by the human on 2026-08-11. It does
not claim the enrollment, invitation, token, or browser-session lifecycle in
S96.

On 2026-08-11 the human approved an immutable `personal | team` kind stored
beside the durable `space_id`. Existing installations migrate to `personal`;
creating a team space must choose `team` explicitly. RCP never infers the kind
from the current process, host, path, presence of credentials, or number of
users.

Every human known to a space has a random immutable `user_id` and a mutable
display name. Names are labels, not identity: two people may choose the same
name without becoming the same person, and renaming never changes the id. A
name is one line and at most 120 characters because it is copied into permanent
history and future task receipts.

A personal space creates one durable local-owner identity without creating team
credentials. Its owner chooses an explicit RCP display name before the first
newly attributed write. A team request receives its acting user from trusted
server admission; the request body never selects a user id.

## Setup

A personal space with no chosen owner name and a team-space store containing two
pre-provisioned member identities with the same display name. The credential and
session setup that authenticated the team members is a test boundary, not a
claim that S96 is implemented.

## Drive

1. Read the stored kind of both spaces, restart both stores, and compare every
   space kind and user id.
2. Attempt to reopen each store while explicitly claiming the opposite kind.
3. Rename one team member and inspect both records.
4. Send an authenticated request as each member, including a forged different
   user id in one request body.
5. In the personal space, read the project, use Discuss, and attempt a Sync
   before choosing a display name.
6. Choose the display name and repeat the write.

## Assert

- `user_id_is_random_immutable_and_durable`
- `space_kind_is_stored_immutable_and_survives_restart`
- `an_existing_installation_migrates_to_personal_kind`
- `opening_a_space_as_the_opposite_kind_fails_closed`
- `display_name_is_user_chosen_mutable_and_not_identity`
- `display_name_is_a_bounded_one_line_history_label`
- `two_people_with_the_same_name_remain_distinct`
- `restart_preserves_personal_and_team_human_identity`
- `the_server_derives_the_acting_user_instead_of_trusting_the_body`
- `a_personal_space_has_one_durable_local_owner`
- `the_personal_owner_name_is_never_guessed_from_the_operating_system`
- `reads_discuss_and_paper_coach_work_before_a_personal_name_is_chosen`
- `patch_capable_actions_refuse_before_the_personal_name_is_chosen`
- `choosing_a_name_enables_future_attributed_writes`

## UI path

The first patch-capable action in an unnamed personal space opens one compact
identity prompt. It explains that the chosen name will be copied into permanent
project history. Cancelling returns to the unchanged draft or run form; it does
not discard the person's work.

The current display name is later editable in Settings. The interface does not
show or suggest the operating-system account name.

## Boundary

This scenario accepts a pre-authenticated team principal. Bootstrap codes,
invitations, token hashing and rotation, session cookies, CSRF defenses, member
removal, and secure desktop credential storage remain in S96 and require their
own confirmation before implementation.

Restart and data-directory relocation preserve the stored space kind exactly as
they preserve `space_id`.
