---
id: S114-see-your-results-without-leaving
status: pending
tier: hermetic
driver: pytest + browser + ssh
covered_by:
  - tests/test_result_view_artifacts.py
  - tests/test_result_view_stage.py
  - tests/test_result_view_storage.py
  - tests/test_result_view_keep.py
  - tests/test_result_view_work.py
  - tests/test_result_view_retry.py
  - tests/test_result_view_api.py
  - tests/test_result_view_discuss_retention.py
  - web/tests/resultViewContracts.test.mjs
  - web/tests/resultViews.test.mjs
invariants: [1, 2, 6, 10e]
---

# See your results without leaving RCP

**Confirmed by the human 2026-08-12:** the two things worth looking at are
curves over steps and samples of generated output; a view is throwaway with an
explicit **Keep**; views live inside Runs; the agent draws the page itself; a
kept view becomes a file in the project's state repository; drawing is an
ordinary Work turn carrying a specific instruction, not a staged package; the
agent names the file descriptively and RCP qualifies that name; the page must be
a file the agent **edits in place** so a revision is cheap; the agent reaches
that file by **resuming its own session**; and the human revises by acting
**on the picture** — boxing a region, underscoring a sample — rather than
describing it in words.

Today, finding out how a run went means leaving. You open a terminal, or a
notebook, or a directory of saved PNGs. RCP holds the graph and the runs but
shows you none of the numbers, so the loop that matters — look, form a hunch,
try something, look again — runs outside the app that knows what the runs were
for.

This scenario is the smallest honest fix: an agent that already has the run's
output files draws you a page, you look at it inside Runs, and it goes away
unless you say otherwise.

## What this is not

It is not a dashboard, and it does not answer *is my machinery working* —
utilization, throughput, and scalar browsing have incumbents and stay out.

It does not make a view part of what the project knows. Nothing here appends a
patch, spends a revision, creates a Proposal, or enters the Inbox. A kept view
is a file that travels beside the research record, not inside it.

## Setup

A project with a completed Experiment run whose repository holds a metrics file
and a directory of generated samples. Canonical state local for the first pass
and remote for the last check.

## Drive — proposal

1. Open **Runs** and open the completed run's detail.
2. In the conversation choose **View → New view**, then ask it to show how the
   run went. The agent reads the metrics file and writes a page with the loss
   curves overlaid across seeds. The selector returns to **No view** after send.
3. Read it inside the run detail, without a new navigation destination
   appearing anywhere.
4. Draw a box around the spike at the end of one curve. The gesture becomes a
   visible draft message naming what was selected. Add "why" and send.
5. The turn resumes the same session, the agent edits the existing page rather
   than drawing a new one, and the view updates in place with no second card.
6. Choose **View → New view** again, then ask for the failure cases. The agent
   writes a second page showing generated samples side by side.
7. Underscore two of the samples and ask what they have in common.
8. Leave Runs, come back, and confirm both views are still there.
9. Press **Keep** on the curves. Leave the samples alone.
10. Wait past the artifact retention window, or force expiry.
11. Reopen the run. The curves are still readable; the samples are gone.
12. Find the kept file in the state repository, read its name, and open it
    outside RCP.
13. Keep a second view on the same day with the same agent-chosen name, and
    confirm neither overwrites the other.
14. Repeat step 9 on a project whose canonical state is on another machine.

## Assert

- `a_view_renders_inside_the_run_detail_and_adds_no_navigation_destination`
- `a_gesture_becomes_a_visible_editable_draft_before_anything_is_sent`
- `a_gesture_alone_never_dispatches_a_turn`
- `a_selection_payload_is_bounded_and_treated_as_untrusted_text`
- `a_page_that_reports_nothing_still_allows_an_ordinary_typed_revision`
- `a_revision_resumes_the_same_session_and_edits_the_existing_file`
- `a_revision_turn_carries_the_view_file_path_not_the_pages_contents`
- `the_view_file_survives_between_turns_of_the_same_conversation`
- `a_lost_session_reports_that_plainly_instead_of_silently_redrawing`
- `an_unkept_view_disappears_when_its_artifact_expires`
- `a_kept_view_survives_expiry_of_the_artifact_it_came_from`
- `keeping_writes_through_the_state_workspace_lock_never_a_direct_file_write`
- `a_kept_view_lands_outside_dot_research`
- `a_kept_filename_carries_the_agents_name_the_project_and_a_yy_mm_dd_suffix`
- `two_keeps_of_the_same_name_on_one_day_never_overwrite_each_other`
- `keeping_appends_no_patch_and_spends_no_revision`
- `keeping_creates_no_proposal_and_changes_no_attention_count`
- `a_kept_view_renders_under_the_same_sandbox_as_a_temporary_one`
- `keep_works_when_canonical_state_is_remote`
- `a_failed_keep_leaves_the_temporary_view_readable_and_says_what_failed`
- `rcp_serves_a_view_from_its_own_stored_copy_rather_than_from_the_stage`
- `a_failed_or_interrupted_revision_never_damages_the_readable_view`
- `a_remote_view_renders_without_reading_its_stage_over_ssh`
- `an_expired_view_takes_its_stored_bytes_with_it`
- `a_gesture_switches_the_composer_into_work_and_shows_that_it_did`

## UI path (proposal)

**Where.** Inside the existing run detail, under the conversation. No new
destination, no new tab. This is what keeps the recorded two-projection rule
intact.

**The view itself.** A card carrying its name and state, with the page rendered
in the sandboxed frame RCP already uses for previews. No caption underneath
explaining what it is — the no-commentary rule applies here as everywhere.

**Revising one — acting on the picture.** The gestures are **box** (drag a
region) and **underscore** (mark one or more items). They are how you point at
what you are looking at instead of describing it: box the spike, underscore the
two bad samples. The gesture resolves to a short description of what was
selected, which lands in the composer as an ordinary draft you can read and
edit. You then add your question and send.

A gesture never dispatches a turn by itself. It writes a draft; you send it.
That keeps every turn something you authorized and read first, and it means a
page that reports a wrong selection is a visible mistake rather than a silent
one.

A gesture does switch the composer into Work, and that is deliberate rather than
incidental: revising a view is a Work turn, so a gesture that left you in Discuss
would compose a turn unable to do the thing you just asked for. It is the one
case where page content changes the composer's mode, so the change must be
visible in the mode label the moment it happens. The page still cannot send,
choose a mode other than Work, or reach anything else in RCP.

**How a gesture gets out of the page, and why it is bounded.** The agent draws
the page, so only the page knows that these pixels are steps 8000–9000 of seed
three. RCP therefore cannot interpret a gesture from the outside, and the page
has to report it. That report is the **only** channel from an agent-drawn page
back into RCP: one-way, a small fixed shape, size-capped, and treated as
untrusted text. RCP exposes nothing to the page in return — no API, no state, no
project data — and the page still cannot navigate RCP, open a popup, submit a
form, or start a download. The isolation that matters is unchanged; what is new
is a bounded outbound message whose worst case is wrong words in a draft you are
about to read.

A page that reports nothing is not broken. Typing a revision by hand still
works, so a view stays useful even when its author did not implement gestures.

**Keep.** One control on the card. Pressing it copies the page into the state
repository through the ordinary workspace path — the same lock and explicit
publish every other canonical write uses — and the card then shows it is kept.
There is no un-keep in this version; deleting a kept view is deleting a file.

**Where kept files land.** A `views/` directory at the state repository root.
Deliberately not under `.research/`, which is append-only history and
materialized outputs, and which no view may touch.

**Names.** The agent chooses a descriptive base name; RCP owns the rest and
qualifies it with the project and a `yy-mm-dd` suffix, so kept files sort by
project and date and read plainly a year later. RCP disambiguates rather than
overwrites when the same base name is kept twice in one day. The agent never
chooses the final filename, because a name it picks freely is a name that can
collide with an existing file in a repository RCP does not own.

## The one thing that must be got right

The page has to be **a file the agent edits**, not a picture it redraws. That
is what makes "same thing but log scale" cheap, and cheapness is the entire
point of the route.

**Confirmed 2026-08-12: the agent reaches the file by resuming its own
session.** Nothing new is needed to make that work. Conversation scratch is
already keyed by project and conversation and deliberately reused, precisely
because a resumed native session must run in the directory it was given. The
view file lives at one stable path under that same conversation workspace, and
RCP serves those exact bytes directly for display. It never switches to or
copies through a turn's artifact directory. A per-turn directory could not hold
it, and putting it there would turn every revision into a redraw.

Resume is the mechanism, not a preference, so its failure has to be honest: if
the session cannot be resumed, RCP says so. It never quietly starts a fresh
session that redraws the page from nothing, because that looks like success and
loses the edit.

## Where the served bytes live — decided 2026-08-12, second pass

The first implementation made the stage file the *only* copy: the page the agent
edits was also the page RCP serves. That one choice paid for a great deal.
Because a failed revision could destroy the readable view, it needed a rollback
subsystem — prior bytes checkpointed into a private snapshot directory in its own
binary format before every revision, restored on rejection or hard interrupt. It
also meant a remote project read its view over SSH on every single request, with
no caching, twice per render.

**RCP now stores the verified bytes itself and serves those.** The stage file
stays what it always was — the working copy the agent edits in place by resuming
its own session, which is the property that makes a revision cheap and is not
changed by this. After a turn, RCP validates the file as before and persists the
bytes alongside the digest and size it already records. Kept and unkept views are
then read the same way, and an episode report sets this precedent by
storing its HTML the same way under the same size cap.

What this deletes rather than documents: the rollback subsystem, in full. A
failed revision can no longer damage anything RCP serves, because it never
touches the served copy. Expiry deletes the stored bytes with the record, so a
disposable view stays disposable.

## Measured outcome

**Measured 2026-08-12 with a real Codex session.** The initial curves page took
565.7 seconds. Revising that exact file in the same native session, conversation
workspace, and path took 218.2 seconds: 38.6% of the initial time, or 2.59x
faster. Atomic replacement changed the inode but preserved the file identity
that matters here — the same stable path and view id — and both turns left their
per-turn artifact directories empty. A separate sample grid took 545.0 seconds,
consistent with a new drawing rather than a revision.

One intervening 191.1-second run was deliberately excluded from the comparison:
its second-turn prompt incorrectly named a new per-turn artifact directory and
did not name the existing view file. The provider followed that instruction and
created a second file. That was the path-contract error this scenario now
forbids, not a provider or native-session limitation.

## Boundary

The predicate in [Q7](../open-questions.md) governs what may be added later:
where the object is discrete and configural — runs, configs, samples, items —
RCP may host the view. Where it is a continuous field or a giant array, RCP
links to the tool that owns it. Curves and sample grids are on the hosted side.
Nothing here authorizes a field viewer, a trace viewer, or a per-domain
connector.
