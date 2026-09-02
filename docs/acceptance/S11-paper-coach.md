---
id: S11-paper-coach
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_api.py::test_paused_paper_coach_resumes_from_task_checkpoint_before_session_record
  - tests/test_api.py::test_paper_resume_rejects_settings_change_before_launch
invariants: [3, 4]
last_checked: 2026-09-01 — focused coverage passed; the full browser journey was not redriven.
---

# The coach reads and never writes

You write; the coach responds. It has no way to write anything, anywhere.

Coverage is thin here. The resume path is tested; the **read-only boundary
itself** is asserted by the permission contract rather than by a test that
watches a coach try to write and fail.

## Setup

A temporary copy of the demo project. Fake agent: replies, and also attempts to
write a `patch.json`, which must go nowhere.

## Drive

1. Open the paper workspace. The editor opens with the authored content already
   in it.
2. Type a paragraph of introduction.
3. Drag the split between editor and coach. Make the coach narrow, then wide.
4. Start a coaching session. Ask for a response on what you wrote.
5. Read the reply. Close the session, reopen the workspace, reload the page.

## Assert — browser, not covered

- `editor_opens_with_authored_content` — not a banner pointing at a canonical
  file
- `split_is_resizable`
- `split_position_persists`
- `draft_survived_reopen`
- `draft_survived_reload`
- `agent_config_reduced_to_provider_name` — one small, non-expandable provider
  box: no model, reasoning, machine, or permission summary
- `no_placeholder_text` — an empty conversation is simply empty; no sample
  prompts, no instructional copy
- `no_apply_control_exists`

## Assert — pytest, partially covered

- `coach_reply_rendered`
- `coach_wrote_nothing` — revision unchanged, zero patches, and the attempted
  `patch.json` was never read as a deliverable — **not covered**
- `permissions_match_the_fixed_contract` — `permissions_for()` is the authority
- `paused_coach_resumes_from_checkpoint` — covered
- `settings_change_before_relaunch_refused` — covered

## Failure means

The read-only boundary leaked, or the writing surface lost your draft. Those are
the two things that would make you stop trusting it with a paper, and the draft
half has nothing defending it.
