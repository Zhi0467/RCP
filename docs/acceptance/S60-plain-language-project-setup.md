---
id: S60-plain-language-project-setup
status: pending — not human-confirmed
tier: hermetic
driver: browser
covered_by: none
invariants: [6, 7]
---

# Add a project with plain-language setup steps

The add-project wizard describes the choices a researcher is making, not the
internal names of RCP's state files, prompt assembly, or patch protocol.

## Drive — proposal

1. Open the project index and choose **New project**.
2. On the first step, see a heading that says the person is naming a project
   and adding a repository. It does not say that they are starting with a
   paper.
3. Move through the wizard. The steps are named **Project**, **Repositories**,
   **Agents**, and **Review**.
4. Confirm that the repository controls use plain labels for the repository's
   display name, project-state location, and default agent source.
5. Confirm that the right-hand summary is titled **Current setup** and uses
   readable labels for project state, repositories, agent sources, and agents.
   It does not call itself a boundary ledger or explain the setup with filler
   copy.
6. Confirm that agent cards describe what each agent can do in user terms, and
   that the review action is called **Check setup**. A failed check must not be
   followed by a heading that claims the project is ready.

## Assert — browser

- `wizard_copy_describes_user_choices` — no visible wizard copy uses the
  legacy phrases `paper-project`, `Start with the paper`, `Truth boundary`,
  `Live boundary ledger`, `Raw prompt inputs`, or `graph patch only`.
- `wizard_review_status_matches_checks` — a failed preflight says what needs
  attention and keeps creation disabled; a passing preflight offers the
  corresponding create or connect action.
- `wizard_setup_behavior_is_unchanged` — repository selection, agent profile
  selection, read-only preflight, confirmation, and create/connect behavior
  still work.
- `wizard_has_no_console_errors`

## Open choice for confirmation

The proposed vocabulary is intentionally small: **Project**, **Repositories**,
**Agents**, **Review**; **Project name**, **Repository label**, **Repository
path**, **Store RCP state here**, **Default source**; **Current setup**; and
**Check setup**. The implementation should keep the underlying manifest,
canonical-state, and patch contracts unchanged.
