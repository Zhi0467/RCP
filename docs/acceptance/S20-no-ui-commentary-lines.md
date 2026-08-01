---
id: S20-no-ui-commentary-lines
status: implemented
tier: hermetic
driver: browser
covered_by: none
last_passed: 2026-07-30 — third pass. Widened twice in one day: from "beneath"
  to any position (the first pass missed five panel kickers), then to cover
  static labels dressed as controls and controls that do nothing.
invariants: []
---

# Primary UI elements stand on their own

Buttons, titles, large labels, card headings, and other primary elements do not
carry commentary explaining what the design already communicates — **above,
below, or as the title itself.**

The first pass of this scenario said "underneath", and passed honestly on that
wording while five panels still carried an uppercase kicker *above* the title —
"Research re-entry", "Project reasoning", "Operating state", "Project dialect",
"Project configuration" — and Settings led with an editorial headline
("Defaults for future agent calls.") in place of a title. Position was never the
point. Restating what the nav tab already said is.

**A panel opened from a named tab does not reintroduce itself.** The tab is the
title. A heading earns its place only by adding a word that means something —
"Research paths" over a tab named Research is fine; "Glossary" over a tab named
Glossary is not.

## UI path — confirmed 2026-07-30

This rule applies to every RCP surface: the project index, project setup,
overview, Research and Runs projections, DAG, glossary and attention views,
settings, run and task dialogs, detail drawers, chat, and the paper workspace.

Remove helper subtitles and descriptive commentary lines beneath primary UI
elements. Do not replace them with tooltips, placeholder text, empty-state
slogans, or the same copy elsewhere. When secondary information is real content
or state rather than commentary, present it as content or metadata instead of a
faint explanatory sentence attached to the primary element.

Actual errors, conflicts, required warnings, live status, user-authored or
agent-authored content, identifiers needed to distinguish records, and
accessibility labels remain explicit.

## Drive

1. Open the project index and inspect existing project cards and the new-project
   action.
2. Walk through every step of project setup.
3. Open a project and visit Overview, Research, Runs, DAG, Glossary, Attention,
   Paper, and Settings.
4. Open the run dialog, task activity and inspector, a node detail drawer, node
   and project chat, and the paper coach.
5. Trigger one validation error or conflict and inspect one active or completed
   run so exception states are visible.
6. Repeat the pass at a narrow viewport.

## Assert

- `no_commentary_line_beneath_primary_ui` — no button, title, large label, card
  heading, modal heading, drawer heading, or section heading has a smaller muted
  explanatory line beneath it
- `no_kicker_above_a_panel_title` — no uppercase eyebrow sits above a panel,
  dialog, drawer, or rail heading to characterize it. An eyebrow carrying real
  data — a node's type, a record kind, the label naming what a number counts —
  stays.
- `no_panel_reintroduces_its_own_tab` — a panel opened from a named tab does not
  restate that name as its heading or headline
- `static_labels_do_not_look_interactive` — a label that does nothing carries no
  outline box and no action-implying icon. A padlock reads as "click to unlock"
  and a file icon as "click to open"; wearing one while doing nothing is a lie
  about the control. Filled, borderless, icon-free tags are fine — they read as
  tags. ("manifest-backed" wore a file-code icon in an outlined box, and
  "membership guarded" wore a padlock; neither was clickable.)
- `no_narration_of_a_fixed_contract` — a permission contract the human cannot
  change is not restated in prose on every surface that uses it. The agent
  permission contract is fixed by invariant 4, so the surface shows at most the
  short tag, never the sentence.
- `every_control_shown_does_something` — a control is rendered only where it has
  an effect, and a control that *does* have an effect is not hidden. Reasoning
  effort reaches both providers (Codex via `model_reasoning_effort`, Claude via
  `--effort`), so both show it. The first attempt at this assertion asserted the
  opposite from memory and hid a working control; what a CLI accepts is read
  from the CLI, and [S24](S24-provider-registry.md) owns that rule.
- `no_displaced_commentary` — removed copy was not moved into a tooltip,
  placeholder, slogan, or decorative empty state
- `real_content_remains_content` — research statements, node bodies, agent
  replies, repository paths, and other records remain readable when needed
- `state_remains_explicit` — counts, identifiers, selection state, progress, and
  other information needed to use or distinguish controls remains available
- `exceptions_remain_explicit` — errors, conflicts, required warnings, and live
  status are still visible
- `accessible_names_remain` — icon-only and visually concise controls retain
  accessible labels
- `no_console_or_application_request_errors`

## Failure means

The interface has slipped back into explaining its own visual hierarchy with
decorative microcopy, or the cleanup hid information a person actually needs to
use the product.
