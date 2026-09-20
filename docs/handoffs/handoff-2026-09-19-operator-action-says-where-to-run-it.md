# A human stop says what to do, and where to run it

Date: 2026-09-19
Status: design confirmed by the human on 2026-09-19 against a rendered mockup of
the redesigned panel. Nothing is implemented. The five decisions below are
settled; the one open question is named in "Open".

Close this handoff when a human stop in project provisioning reaches the
operator as an ordered list of single actions, every command in it names the
shell it runs in, the deploy-key grant is a two-field copy form titled for the
human's task, and the same execution context appears in the interactive CLI
wizard.

## The problem

A human stop in server setup is rendered as a flat pile of strings. The human
who hit this during a real project setup reported, in order:

1. They did not know that the first thing to do was run a command in their own
   terminal. Nothing on the panel says a command is theirs to run, or where.
2. The command they were given bundled the SSH hop and the remote command into
   one blob. They wanted the hop and the command to run *there* kept apart.
3. The panel renders instruction and command lines as long vertical ribbons
   down the left quarter of the card.
4. With a deploy key prepared, it was unclear how to set one up — although the
   real job is copying exactly two values into a GitHub form.
5. After GitHub, a resume command sits at the bottom with no statement that it
   should be run, where to run it, or what to do afterwards.

Each has a concrete cause.

**The card is titled after the machine's next check, not the human's task.**
[`_copy_operator_contract`](../../src/rcp/server_ops/project_provision.py) keeps
the pending plan step's `title` and `purpose` and copies only `message`,
`actions`, `fields`, and `resume_argv` from the operator step. The grant step in
[`git_credentials.py`](../../src/rcp/server_ops/git_credentials.py) is titled
"Grant repository write access"; that title is discarded, so the human reads
"Verify Git write access for <alias>" — a sentence about what RCP will do next —
above a form that is theirs to fill in.

**Nothing in the contract says where a command runs.** `CommandAction` in
[`server_ops/models.py`](../../src/rcp/server_ops/models.py) is `{kind, argv}`
and `ServerStep.resume_argv` is a bare argv tuple. Every renderer therefore
prints a command with no frame. In the desktop that frame is genuinely missing
information: the desktop invoked `rcp server project provision` for the operator
over the saved SSH route, so the operator never typed a shell prompt and has no
reason to know one exists, let alone which host the printed `sudo` line belongs
to. The interactive CLI wizard has the same gap in milder form: it prints
`  1. $ sudo -u …` with no statement that this is the same shell.

**The panel has no order and no affordances.**
[`OperatorAction`](../../web/src/views/TeamProjectSetup.tsx) renders, in one
undifferentiated run: a four-cell metadata grid, a bare link, every action in
`actions`, every entry in `fields` labelled with its raw snake_case name, and
finally `resume_argv` under the word "Resume". No copy control exists anywhere
in the panel, no item is numbered, and the two values that must reach GitHub
(`deploy_key_label`, `deploy_public_key`) are styled identically to
`public_key_fingerprint`, which is only to be compared by eye.

**The ribbons are one CSS bug.** `.operator-action-line` in
[`styles.css`](../../web/src/styles.css) is a two-column grid,
`minmax(90px, 0.28fr) minmax(0, 1fr)`. Labelled rows need both columns. Action
lines render a *single* child, which lands in the 28% column and wraps down it
while the rest of the card stays empty.

**The SSH hop is baked into one argv.** For a repository on an `ssh` machine,
`github_trust_argv` returns the remote command `shlex.join`-ed inside the ssh
invocation. One opaque string where the operator wanted two named pieces.

## Decided on 2026-09-19

1. **A command action names the shell it runs in.** `CommandAction` gains a
   required execution context; `ServerStep` gains the same context for
   `resume_argv`. This is a shared contract across
   [`server_ops/models.py`](../../src/rcp/server_ops/models.py),
   [`web/src/types.ts`](../../web/src/types.ts), and the stored step records in
   [`storage/models.py`](../../src/rcp/storage/models.py), so it lands serially
   and first. Every construction site states its context explicitly; there is no
   default. Rejected: letting the web client infer the host from
   `machine.location`, which is the guessing that produced this panel.
2. **The context names the machine, and the client supplies the way in.** The
   server knows which host and service account a command concerns; only the
   desktop knows the operator's own SSH target, which lives in the saved
   operator route. The panel renders the context as a label on the command block
   and composes the entry line from the saved route beside it. Neither layer
   invents the other's fact.
3. **The SSH hop is never bundled into the command the operator reads.** Where a
   step crosses a machine boundary, the hop and the command that runs there are
   separate, separately copyable pieces.
4. **A human stop renders as an ordered list of single actions.** One numbered
   step per action, each with a heading. `purpose` and `expected_success` move
   behind one explicit disclosure; `performed_by` is dropped from the body,
   because the card already says a human is required. This follows
   [interface and visual design](../specs/interface-and-visual-design.md): no
   muted commentary line under a heading, and a read-only inspector rather than
   a caption when there is more to say.
5. **The operator stop keeps its own title.** `_copy_operator_contract` takes
   `title` and `purpose` from the operator step, not the pending plan step, so
   the card is named after the human's task. The planned step's typed target
   check is unchanged.

## Plan

Four slices. The first is the shared contract and lands alone.

1. **Contract.** Add the execution context to `CommandAction` and to
   `ServerStep`'s resume command; update every construction site in
   `server_ops/` (`git_credentials.py`, `members.py`, `provider_readiness.py`,
   and any restore path) to state it; mirror it in `web/src/types.ts`; keep the
   existing `_StrictModel` validation and the secret-shaped-flag refusals.
   Update the persisted step record and any replay of stored steps.
2. **Operator stop titles.** Take `title` and `purpose` from the operator step
   in `_copy_operator_contract`, and give the deploy-key grant step and its
   restore twin titles that name the human's task.
3. **Web panel.** Rebuild `OperatorAction` as the ordered list: numbered steps,
   copy controls on every command and on each deploy-key value, the execution
   label and entry line, the grant rendered as a titled two-field form with
   *Allow write access* as an explicit requirement, the resume command as the
   final step followed by Refresh. Replace the `.operator-action-line` grid with
   full-width blocks and keep the two-column grid only for labelled rows.
4. **CLI wizard.** Render the same execution context in `_render_actions` and in
   the `Continue:` block, so the terminal operator reads the same frame.

## Verification

- `uv run pytest -n0` over the affected `server_ops` and API projection tests,
  including the step-contract validation tests and any golden machine-readable
  event records.
- `node --experimental-strip-types --test web/tests/<affected>.test.mjs` and
  `npm --prefix web run build`.
- `uv run ruff check` and `uv run pre-commit run --files` over changed paths.
- The served-app journey: drive a provisioning request to the deploy-key stop on
  a throwaway server with a disposable data directory, and read the rendered
  panel in the browser. The human's own server and data directory are never used.
- The interactive CLI wizard's rendering of the same stop, captured from a
  terminal run rather than asserted only in a unit test.

## Open

One question for the design review: whether the resume command should become a
`CommandAction` in `actions` carrying its own context, rather than `ServerStep`
growing a second context field beside `resume_argv`. The contract already
forbids a step from carrying actions without a resume command, and the CLI
renderer already skips an action whose argv equals `resume_argv`, so the two are
near-duplicates today. Folding them would be a larger change to a contract that
several call sites and stored records depend on; keeping them apart adds one
field. Decide before slice 1.
