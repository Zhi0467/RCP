---
id: S21-compact-project-navigation
status: implemented
tier: hermetic
driver: browser
covered_by: none
invariants: []
last_passed: 2026-07-30 — agent-driven at desktop and 390px against a throwaway demo project
---

# The project shell says only what is needed

## UI path — confirmed 2026-07-30

Open any project. The project header contains the back control, without a
project name, RCP wordmark, product logo, or revision label. The project-panel
navigation owns the fold control immediately to the left of Overview.
Sync keeps its label. Agent task history and Refresh are icon-only controls with
explicit accessible names; project chat is labeled **Ask**. The controls form two semantic groups:
the labeled actions **Sync / Ask** sit together, followed by the icon utilities
**History / Refresh** together. They are not four evenly spaced peers.

The primary navigation reads **Overview**, **Inbox**, **Research**, **Runs**,
**Paper**, **Settings**, and **Chats**. Inbox carries its current attention count
in a colored badge. DAG is not a primary tab: Research owns a compact
**Research / DAG** subpanel switch. Glossary terms appear inline where they are
read rather than as a primary destination.

## Drive

1. Open a project at desktop and narrow widths.
2. Inspect the header controls and primary navigation.
3. Open Inbox and confirm its badge matches pending Proposals, open Ambiguities,
   and every open Blocker regardless of subtype.
4. Open Research, switch to DAG, then switch back to the path projection.
5. Use the icon-only task and Refresh controls, and open Ask.

## Assert

- `header_has_no_product_mark_or_revision`
- `task_and_refresh_controls_are_icon_only_and_accessible`
- `sync_and_ask_form_the_labeled_action_group`
- `history_and_refresh_form_the_icon_utility_group`
- `project_chat_is_ask`
- `inbox_has_colored_attention_count` — the badge includes every open Blocker
- `dag_is_a_research_subpanel`
- `glossary_is_not_a_primary_destination`
- `narrow_layout_preserves_every_control`
- `no_console_or_application_request_errors`

## Failure means

The project shell has accumulated redundant branding or labels, flattened a
real panel hierarchy, hidden attention state, or made concise controls
inaccessible.
