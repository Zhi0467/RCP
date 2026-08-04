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
  - web/tests/skillPicker.test.mjs
last_passed: 2026-08-04 — browser drove the Settings card grid, the read-only
  package inspector, a default save persisted to the manifest, and the keyboard
  `/` picker (arrows, Enter-selects-without-sending, Escape) in project chat and
  paper coaching; 626 backend and 165 web checks passed.
invariants: [4, 4b, 8, 9, 10, 10b, 10c]
reported_by: human, 2026-08-04
---

# Choose project workflows and skills, then load them into a run

RCP ships an official, source-versioned registry of skill folders and
prompt-level workflow folders. The catalog is always available in the app; the
user does not install, edit, or pull packages back from a run. Project Settings
stores the selected default ids only. A task may add or remove any number of
workflows and skills for its current turn.

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
5. Open a project or node chat. In the composer, type `/` or `$`. A dropdown
   shows the official workflows and skills, filtered as the trigger word is
   typed. Up and down arrows move the highlight, Enter selects the highlighted
   entry, and Escape closes the dropdown without sending. While the dropdown is
   open, Enter never sends the turn. Selected entries appear as removable
   composer chips. These selections apply only to the current turn and do not
   change the project defaults.
6. Send the turn in either Discuss or Work mode. The captured mode remains the
   authority: selecting a skill never grants graph or repository permissions.
7. Open the resulting Agent task. Its immutable contract shows the selected
   workflows, the resolved dependency skills, their versions, and compact
   pointers to the staged folders. It does not embed their bodies.
8. Start a Seed or Refresh task. Its launch surface begins with the project
   defaults and allows the same multi-selection. A selected workflow is
   preflighted before provider launch; its workflow file and every declared
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
- RCP stages the selected workflow folders and resolved skill folders into the
  per-run local or remote temporary workspace. The bytes are immutable for the
  task, are never copied into `.research` or a project repository, and are
  disposable after successful completion. A failed task retains its stage under
  the existing recovery policy.
- Context assembly and the task contract expose compact ids, versions,
  descriptions, and exact staged paths. The agent reads the files in place;
  RCP does not parse workflow prose into a second execution engine.
- **The official registry is authoritative, not the task's saved snapshot.** A
  task stores the selected ids; every launch — first attempt, retry, or resume —
  re-resolves those ids against the registry as it stands at launch and stages
  those bytes. Upgrading a package therefore upgrades the next attempt of an
  existing task, deliberately. A stored version is a receipt of what an attempt
  ran with, never an input that could pin, downgrade, or fail a later attempt.
- Because each attempt stages its own bundle, a resumed native session never
  reports a version its staged folder does not contain.
- Discuss, Work, Seed, Refresh, and paper coaching may all select a loaded
  package. The selection does not alter `permissions_for()` or create another
  graph-change channel.
- v1 contains only official RCP packages. Users cannot author or import skills
  through the UI.

## Assert

- The Settings catalog and task launch controls are backed by one registry.
- Project defaults, current-turn selections, and per-attempt receipts are
  distinct records with clear precedence.
- A catalog card shows its name and selection state only; package text is
  reachable through an explicit read-only inspector, never as caption copy
  under the card.
- The slash/dollar interaction produces structured selection metadata rather
  than relying on the provider to parse chat text, and it is fully operable
  from the keyboard: arrows highlight, Enter selects, Escape dismisses, and an
  open dropdown suppresses send.
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

The catalog is only decorative, slash selection is lost on the wire or is
mouse-only, workflow dependencies are silently omitted, an upgraded package
makes an existing task un-retryable, an attempt reports a version its staged
folder does not contain, skill content enters `.research`, selected skills
widen permissions, or the agent must infer selection from the human's raw
message.
