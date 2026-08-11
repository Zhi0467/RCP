---
id: S110-paper-draft-survives-a-canonical-change
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_paper.py
  - tests/test_storage.py
  - web/tests/paperAndChatProfile.test.mjs
  - browser 2026-08-11 — isolated external canonical edit, Incoming, Apply, repin
invariants: [2, 6, 7]
last_passed: 2026-08-11 — the isolated served Paper view preserved typed text,
  detected an external canonical change, rendered Incoming, swapped both versions
  twice, and cleared behind only after a later edit saved successfully
---

# A paper draft survives a canonical change without choosing a side

The canonical introduction can change while someone is writing against it.
Today that stops autosave and offers two buttons, each of which destroys one
version. Neither destruction is acceptable, and neither is a stalled editor.

## UI path — confirmed 2026-08-11

- When canonical moves off the draft's base, the editor keeps the human's text
  exactly as typed and autosave does not stop. The panel marks itself **behind**.
- The existing Write / Preview toggle gains a third segment, **Incoming**,
  present only while behind. It renders canonical in the same pane, using the
  same renderer as Preview. No second document, no side-by-side editor, no
  modal.
- One **Apply** control swaps the two: canonical moves into the editor, the
  replaced text moves into the Incoming pane. Pressing Apply again swaps back.
- Saving after any edit re-stamps the draft's base to the canonical it was shown
  against, and the panel leaves the behind state.
- Deliberately removed: the conflict banner with **Use canonical** and
  **Overwrite canonical**. Neither outcome is reachable as a single destructive
  choice any more.
- Deliberately not possible: canonical replacing the editor's contents without
  the human acting, and a save landing against a canonical version the human was
  never shown.

## Drive

1. Open a project's Paper view and type several sentences without leaving the
   editor.
2. Change the canonical introduction from outside the app.
3. Confirm the panel marks itself behind, the typed text is untouched, and
   autosave has not stopped.
4. Open Incoming and confirm canonical renders in the same pane.
5. Press Apply, confirm the editor now holds canonical and Incoming holds the
   replaced text, then press Apply again and confirm the swap reverses.
6. Edit one sentence and save. Confirm the save succeeds and the behind state
   clears.

## Assert

- `canonical_change_never_replaces_editor_content`
- `autosave_continues_while_behind`
- `behind_state_is_visible_in_the_paper_panel`
- `incoming_segment_appears_only_while_behind`
- `incoming_renders_in_the_same_pane_as_preview`
- `apply_swaps_canonical_and_draft_without_losing_either`
- `save_after_edit_repins_the_base_and_clears_behind`
- `save_never_lands_against_an_unshown_canonical`
- `no_use_canonical_or_overwrite_canonical_control_exists`

## Failure means

A human loses paragraphs they typed, an editor stalls with no way forward but
destroying one version, or a save silently overwrites a canonical introduction
its author never saw.

## Notes

Showing *which passage* changed needs the common ancestor, which
`paper_drafts` does not store — it keeps `content` and `base_hash` only. Adding
an ancestor column is part of this scenario's implementation, through
`_ensure_column`, with any index declared in the migration block below those
calls rather than in the `CREATE TABLE` script. Verify the migration against a
copy of a real store; a fresh test database proves nothing about it.

Storing an ancestor and diffing against it is merge-shaped but is not version
control: there is one linear canonical file, nothing branches, and the only
reconciliation is of an uncommitted draft against a file that moved.
