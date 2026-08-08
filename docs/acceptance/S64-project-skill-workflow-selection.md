---
id: S64-project-skill-workflow-selection
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_skill_registry.py
  - tests/test_api.py::test_seed_stages_its_selected_skills_and_records_what_it_ran
  - tests/test_api.py::test_an_upgraded_package_never_makes_a_stored_task_un_retryable
  - tests/test_api.py::test_retrying_a_failed_seed_records_the_selection_it_will_stage
  - tests/test_prompts.py::test_structured_invocation_activates_exact_pointer_without_rewriting_human_message
  - tests/test_chat_prompt_protocol.py
  - web/tests/skillPicker.test.mjs
last_passed: 2026-08-08 — Settings exposed Experiment causality, the browser
  drove its slash selection into a real Work turn, the task inspector showed the
  untouched slash message beside the separate exact activation pointer, and a
  later turn does not retain that invocation; browser console and server log
  were clean
invariants: [4, 4b, 8, 9, 10, 10b, 10c]
reported_by: human, 2026-08-04
---

# Choose project workflows and skills, then load them into a run

RCP ships an official, source-versioned registry of skill folders and
prompt-level workflow folders. The catalog is always available in the app; the
user does not install, edit, or pull packages back from a run. Project Settings
stores the package ids that every run may stage. A chat or paper turn may
explicitly invoke a subset of those packages with slash syntax.

## UI path

1. Open a project and choose **Settings → Skills & workflows**.
2. Browse the official catalog as a grid of cards. A card carries its name and
   its selected/unselected state — nothing else. Selecting a card is one click;
   opening it is a separate control.
3. Open a card to inspect the package. The inspector is read-only and shows the
   package's own text: kind, version, description, declared dependencies, and
   the full `SKILL.md` or `WORKFLOW.md` body. There is no install, edit,
   import, or authoring action anywhere in this view.
4. Select a workflow and an additional skill as project defaults, then save.
   The project stores ids, not skill bodies or staged paths.
5. Open a project or node chat. The composer shows no persistent skill or
   workflow chips. Type `/`. A dropdown shows only workflows and skills
   enabled in Settings, filtered as the trigger word is typed. Up and down
   arrows move the highlight, Enter selects the highlighted entry, and Escape
   closes the dropdown without sending. While the dropdown is open, Enter never
   sends the turn. The selected entry is transient invocation metadata for this
   turn; it is not rendered as a removable chip and does not change Settings.
   The literal slash token remains unchanged in the visible and persisted human
   message.
6. Send the turn in either Discuss or Work mode. The captured mode remains the
   authority: selecting a skill never grants graph or repository permissions.
7. Open the resulting Agent task. Its immutable contract shows the Settings-
   enabled workflows, resolved dependency skills, their ids and versions,
   descriptions, and pointers to the staged folders. It does not embed their
   bodies. A short, separate **Invoked this turn** block names the exact package
   selected by the slash picker and its staged pointer, while the unchanged
   slash token remains in the human message the agent reads.
8. Start a Seed or Refresh task. It stages only the project defaults from
   Settings; there is no separate per-run skill selector. A selected workflow
   is preflighted before provider launch; its workflow file and every declared
   skill dependency are staged together.
9. Open Paper. The writing-coach composer supports the same current-turn
   workflow and skill picker, without changing the project defaults.

## Resolution and staging contract

- A workflow declares dependencies in a required frontmatter section using
  exact official registry ids and versions.
- RCP validates ids, versions, paths, dependency cycles, and duplicate-version
  conflicts before launching the provider. A selected missing or invalid
  dependency is a visible preflight failure and never becomes a silent partial
  run.
- Multiple workflows are allowed. Shared dependencies are staged once. Two
  selected versions of the same skill fail preflight and require the user to
  choose a coherent set.
- RCP stages the Settings-enabled workflow folders and resolved skill folders
  into the local or remote temporary workspace. Ordinary chat sessions reuse a
  content-addressed immutable bundle while the enabled ids and versions are
  unchanged; other tasks retain per-attempt staging. The bytes are never copied
  into `.research` or a project repository and remain disposable scratch.
- A slash invocation may name only a workflow or skill enabled directly in
  Settings. It never stages an additional package and never grants authority.
  The invocation reaches the agent both as the unchanged literal token in the
  human message and as one short activation block naming the exact invoked id,
  version, and staged pointer. The block requires the agent to read and follow
  that package for the turn; it never embeds the package body.
- Without a slash invocation, the task still receives lightweight pointers to
  every Settings-enabled package. Each pointer includes the description that
  defines its trigger; the agent reads a package only when the task and intended
  change match that description.
- The first chat master context and non-chat task contracts expose compact ids,
  versions, descriptions, and exact staged paths. A changed enabled set or
  package version becomes a compact chat context delta; unchanged pointers are
  not resent. The agent reads files in place, and RCP does not parse workflow
  prose into a second execution engine.
- **The official registry is authoritative, not the task's saved snapshot.** A
  task stores the selected ids; every launch — first attempt, retry, or resume —
  re-resolves those ids against the registry as it stands at launch and stages
  those bytes. Upgrading a package therefore upgrades the next attempt of an
  existing task, deliberately. A stored version is a receipt of what an attempt
  ran with, never an input that could pin, downgrade, or fail a later attempt.
- A resumed native session never reports a version its staged folder does not
  contain. Package changes stage a new immutable bundle before its delta is sent.
- Discuss, Work, Seed, Refresh, and paper coaching receive the Settings-enabled
  package set. Discuss, Work, and paper coaching may explicitly invoke one of
  those packages with slash syntax. Invocation does not alter `permissions_for()`
  or create another graph-change channel.
- v1 contains only official RCP packages. Users cannot author or import skills
  through the UI.

## Assert

- The Settings catalog and task launch controls are backed by one registry.
- Project defaults, current-turn selections, and per-attempt receipts are
  distinct records with clear precedence.
- A catalog card shows its name and selection state only; package text is
  reachable through an explicit read-only inspector, never as caption copy
  under the card.
- The slash/dollar interaction produces structured invocation metadata rather
  than relying on the provider to parse chat text, resolves only to
  Settings-enabled packages, and is fully operable from the keyboard: arrows
  highlight, Enter selects, Escape dismisses, and an open dropdown suppresses
  send.
- Chat and paper coaching do not render persistent skill/workflow chips. The
  task contract and inspector remain the visible receipt of staged packages, and
  the persisted human message is the receipt of what was invoked.
- The provider contract separately names every explicitly invoked package and
  its exact staged pointer, requires its use for that turn, preserves the human
  message byte-for-byte, and leaves unrelated enabled packages as pointers.
- Retrying a task after its package was upgraded runs the new version and says
  so, rather than failing on the older recorded version.
- Workflow dependency preflight catches missing entries, cycles, invalid paths,
  and incompatible versions before provider launch.
- Local and remote staging contains the same selected files and no unrelated
  registry packages.
- A selected package cannot widen the captured Discuss, Work, Seed, Refresh, or
  paper-coach capability.
- A historical task remains inspectable by id and version after a newer package
  version is registered.

## Failure means

The catalog is only decorative, slash invocation is lost on the wire or is
mouse-only, a slash command can stage a package not enabled in Settings,
workflow dependencies are silently omitted, an upgraded package makes an
existing task un-retryable, an attempt reports a version its staged folder
does not contain, skill content enters `.research`, selected skills widen
permissions, persistent chips misrepresent the defaults, or the agent must
infer invocation from the human's raw message, or an invocation marker embeds a
package body instead of pointing to the staged package.
