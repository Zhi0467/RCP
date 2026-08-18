---
id: S101-project-membership
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_project_membership.py
  - tests/test_storage.py::test_project_record_deletion_is_atomic_complete_and_project_scoped
  - web/tests/canonicalRevisionRefresh.test.mjs
  - browser 2026-08-15 — an open project whose membership was removed closed its
    own tab, returned to the index, and left the project absent from the list;
    no console JS error and no server traceback or 5xx
last_passed: 2026-08-15 — creator seating, the one-shot backfill against a store
  built before membership existed, an identical 404 for a non-member and an
  unknown id, a filtered index and Experiment board, all 55 project-scoped routes
  carrying the gate, and replay with no membership records at all
invariants: [1, 3, 4]
---

# Being in the lab is not being on the project

**Confirmed by the human 2026-08-15**, in a grilling session that also split this
scenario. This half is the **boundary**: membership exists, is seeded, and is
enforced. Granting it to a second person — invitations, accept, decline, leave,
and removal — is [S122](S122-project-invitations.md), and everything that can
only happen when membership *changes* lives there.

The current contracts are in
[Project membership and invitations](../specs/projects-spaces-and-operations.md#project-membership-and-invitations)
and [Human identity and project membership](../specs/authority-and-proposals.md#human-identity-and-project-membership).

Space enrollment and project membership are different layers. Joining the lab's
RCP does not admit you to every project in it. Today it does, and that is the
gap: [S96](S96-joining-a-team-space.md) authenticates people and then hands each
of them everything.

## Decided 2026-08-15

- **Two enforcement points, not one.** A request-level dependency covers reads
  and human dispatch; `require_apply` checks separately, because Apply runs
  outside the request under the canonical append lock and
  `AgentTaskAuthority` already carries `project_id` and `authorized_by`.
  `require_dispatch` is left alone — it receives no user and no project, and
  widening it to carry them would be a shared-contract change that still does
  nothing for reads.
- **Creating a project seats its creator.** Both creation routes resolve the
  acting user and write the first membership row. Without this, a membership gate
  locks people out of projects they just made.
- **Membership binds the durable `user_id`, not a display name.** `acting_user`
  returns one without demanding a name, so first-run is unchanged: a name is
  required before an attributed write, not before existing.
- **A personal space has exactly one member.** The owner is seated the same way,
  so the check is one query and there is no personal-space branch to fall
  through.
- **Projects that predate this backfill every current space member, once.**
  Nothing records a project's creator today, so there is nothing to seed from.
  This fails open exactly once, at migration, because failing closed locks a team
  out of its own projects and there is no administrator rank to undo it.
- **A non-member sees nothing.** The project list and the cross-project
  Experiment board both filter. That board reads node titles out of every
  project's cached graph, so leaving it unfiltered would publish research, not
  just names.
- **404, never 403.** A refusal answers exactly as an unknown project id does.
  A 403 would confirm the project exists and undo the line above.
- **The gate is structural.** Every project-scoped route moves onto one
  `APIRouter` that declares the dependency once, and a test walks the app's
  routes to catch any `{project_id}` path declared outside it.
- **A project is never left with no members.** Found during implementation, not
  in the original plan: a team project opened from a console, or opened by the
  server before anybody enrolled, has no creator to seat. Seating nobody is the
  worst outcome — the project is invisible to every member and nobody can invite
  themselves to it, so it can never be recovered. An unclaimed project is
  therefore claimed by whoever is there: everyone present at registration, or
  the first person to enrol afterwards. Once a project has any member,
  invitations govern it and this rule never fires again.

## Setup

A team space with two enrolled members and one project created by the first. A
second store whose projects predate membership. A personal space as the control.

## Drive

1. As the first member, create a project. Read who is a member of it.
2. As the second member, list projects and open the cross-project Experiment
   board.
3. As the second member, request that project by its exact id — read it, dispatch
   into it, and hold a patch to Apply against it.
4. Open the store whose projects predate membership. Read who is a member of each.
5. As the first member, open the project in a tab, then delete the project from
   another window and watch the tab.
6. Replay the project from its patch log with membership records unavailable.
7. In the personal space, create a project and take a patch-capable action
   without ever choosing a display name.
8. Enumerate the running app's routes.
9. Open a project in a team space from the console, before anybody has enrolled.
   Enrol, then read the project list.

## Assert

- `creating_a_project_seats_its_creator_as_the_first_member`
- `membership_binds_the_durable_user_id_and_never_a_display_name`
- `a_personal_space_project_has_exactly_one_member`
- `creating_a_project_still_requires_no_display_name`
- `projects_predating_membership_backfill_every_current_space_member_once`
- `the_backfill_runs_once_and_is_not_reapplied_on_later_starts`
- `a_non_member_project_is_absent_from_the_project_list`
- `a_non_member_project_is_absent_from_the_cross_project_experiment_board`
- `an_exact_non_member_project_id_answers_404_and_never_403`
- `a_non_member_dispatch_never_launches_a_provider`
- `a_non_member_patch_is_refused_at_apply_under_the_append_lock`
- `require_dispatch_is_unchanged_and_carries_no_membership_argument`
- `every_project_scoped_route_is_declared_on_the_membership_router`
- `a_project_scoped_route_declared_outside_the_router_fails_the_route_test`
- `replay_succeeds_with_no_membership_records_present`
- `deleting_a_project_takes_its_membership_with_it`
- `a_team_project_is_never_left_with_no_members`
- `an_open_tab_whose_project_becomes_unreadable_closes_itself`

## UI path

**Nothing is added.** A project you are not on is simply absent from the index
and from the Experiment board — no locked card, no greyed row, no explanation of
something you cannot see.

The one visible behavior is the tab. Project tabs poll their cached revision
every three seconds; a tab whose project stops being readable closes itself and
returns to the index with a plain line saying the project is no longer
available. That is the same path a deleted project already takes.

## Boundary

**Membership is authority inside RCP, not confidentiality on disk.** A project's
canonical state is a git repository, and agents read it by path. Putting those
repositories under the space's own operating-system account is
[S102](S102-team-runs-execute-as-the-space-account.md), which is unbuilt. Until
it lands, a lab member with a shell on the machine can read any project's
`.research` regardless of membership. This scenario does not claim otherwise.

Membership is operational, not canonical. It lives in SQLite and never in
`.research/`. Losing membership never rewrites, annotates, or invalidates
attribution that was truthful when it was written
([S99](S99-attribution-travels-with-history.md)), and replay must keep succeeding
with no membership records at all —
[S100](S100-permission-is-checked-twice.md) already asserts that and must stay
green.

Every member has the same authority. There is no owner, no admin, and no project
role above member. Where real privilege is needed, RCP borrows the operating
system's.
