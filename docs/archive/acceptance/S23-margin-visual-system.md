---
id: S23-margin-visual-system
status: implemented
tier: hermetic
driver: browser
covered_by: none
invariants: []
last_passed: 2026-07-30 — agent-driven across the project shelf and every project surface at desktop and 390px, including the unified project-index/setup mark
---

# RCP uses Margin's visual system

## UI path — confirmed 2026-07-30

Open the project index or any project surface. RCP shares Margin Dev's visual
grammar: paper, paper-deep, and sheet surfaces; walnut text; oxblood primary
actions; restrained semantic accents; compatible editorial, UI, and mono type
stacks; warm rules and shadows; and tactile bound-book cover materials.

This is a coherent product-family treatment, not a literal copy of Margin's
catalog. RCP does not rotate project cards through decorative colors, import
every Margin variant, introduce a separate cold-blue base palette, or retain
isolated blue draft controls. Project covers share one restrained oxblood base
and may differ by texture. Other accent colors appear only when they communicate
meaningful type or state. Layout, labels, navigation, and behavior remain as
already specified.

## Drive

1. Open Margin Dev and record its paper, control, text, semantic-state, type,
   shadow, and material treatment.
2. Open RCP's project index and confirm its background, project cards, controls,
   typography, and materials form one restrained system.
3. Open a project and visit Overview, Inbox, Research, DAG, Runs, Paper,
   Settings, and Chats; open Ask and the run dialog.
4. Repeat the project index, shell, and one dense workspace at a narrow width.

## Assert

- `margin_grammar_is_the_only_visual_system` — rendered RCP surfaces share
  Margin's paper, sheet, walnut, oxblood, semantic color, type, rule, shadow,
  and material language instead of a competing cold or brown reinterpretation
- `project_covers_do_not_rotate_decorative_colors` — every project cover shares
  the restrained oxblood base; texture may vary without creating a color shelf
- `cover_materials_feel_tactile` — dye, mosaic, wood, marble, and diffusion are
  material treatments rather than unrelated theme colors
- `all_surfaces_share_one_system` — the project index, shell, dialogs, DAG,
  paper workspace, inputs, selections, status states, and focus treatment no
  longer switch between unrelated palettes
- `brand_is_one_mark` — project-index and setup branding show one unified RCP
  mark, never an R initial beside a second full RCP wordmark
- `layout_and_behavior_are_unchanged`
- `status_and_accessibility_remain_clear`
- `desktop_and_narrow_views_have_no_overflow`
- `no_console_or_application_request_errors`

## Failure means

RCP does not feel like the same restrained product family as Margin, project
cards turn into a decorative color sampler, or one surface still reveals the
old cold-blue or brown visual layer.
