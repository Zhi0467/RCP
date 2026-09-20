# A human stop says what to do, and where to run it

Date: 2026-09-19
Status: design confirmed by the human on 2026-09-19 against a rendered mockup of
the redesigned panel, revised the same day after an xhigh design review whose
findings were verified against the code, then implemented. All four slices have
landed: the execution context and its call sites, the operator stop's own title
with both event validators relaxed, the rebuilt desktop panel reused by the
transfer view, and the CLI wizard's matching label. Focused Python, web, and
browser checks pass, and the rebuilt panel was driven and inspected against a
stop produced by the real builder.

What remains: the desktop-only half of the served journey. The panel and its
stored stop are verified end to end, but the `ssh` entry line is composed from
the saved operator route, which only the Tauri runtime supplies, so it was
exercised through the fixture rather than through a running desktop.

Close this handoff when the rebuilt panel has been driven against a provisioning
request paused on a live server. Everything it promised is built: a human stop
reaches the operator as an ordered list of named single actions titled for
their task, every command in it names the shell it runs in, the deploy-key
grant is the two-field form with *Allow write access* as its own requirement,
and the interactive CLI wizard says the same things.

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
`  1. $ sudo -u …` with no statement of which shell that is.

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

**The desktop hides the hop and then shows it whole.** The desktop runs the
provisioning command for the operator over the saved SSH route, so the operator
never sees a shell. When a command does surface, it surfaces as one
`ssh <target> sudo … rcp …` string. The hop and the command that runs on the
other end are never named as two things.

## Decided on 2026-09-19

1. **A command action names the shell it runs in.** `CommandAction` gains an
   execution context, and `ServerStep` gains `resume_execution` beside
   `resume_argv`. This is a shared contract across
   [`server_ops/models.py`](../../src/rcp/server_ops/models.py),
   [`web/src/types.ts`](../../web/src/types.ts), and the persisted
   `operator_action` on `ProjectProvisioningRequestRecord` in
   [`storage/models.py`](../../src/rcp/storage/models.py), so it lands serially
   and first. Rejected: letting the web client infer the host from
   `machine.location`, which is the guessing that produced this panel. Also
   rejected: folding the resume command into `actions`. `resume_argv` carries
   semantics that "the last command" cannot — restore offers mutually exclusive
   confirmation commands — and folding would change the request binding, both
   interactive runners, the native validator, and every resume consumer for no
   gain. Deduplicating a resume-equal action now compares argv *and* context.
2. **The context is optional on the wire, and absent means unstated.** A
   required field would be a breaking change across two boundaries that a panel
   fix has no business breaking. The
   [separately versioned supervisor](../specs/server-and-machine-operations.md)
   emits its own operator steps as raw dicts, which the application parses
   strictly through `ServerStepEvent`, so a required field would make a current
   RCP refuse an installed older supervisor. Stored paused provisioning requests
   would likewise stop decoding. Absent context therefore renders exactly as the
   panel renders today: no execution label. Every operator stop RCP itself
   builds states its context, asserted per builder, so the optionality is a
   compatibility boundary rather than a silent fallback. There is no sweep that
   proves a future builder cannot forget; each one is covered by its own test.
3. **The context names the shell, not the operation's target.** `MachineTarget`
   is the machine an operation acts on; for a local machine it deliberately
   carries an empty host and the service account, while the operator logs in
   under their own name and the command inserts `sudo`. The context is its own
   discriminated model of one variant, `server_shell`, naming the account the
   shell must already belong to; a `shell_account` of null is the operator's own
   login, which the command elevates from itself. That is distinct from an
   absent context, which per decision 2 means unstated and renders as it did
   before. No SSH variant was built: every argv RCP displays runs in the shell
   where the server command itself ran, so a second variant would have been
   speculation. The desktop composes the entry line from the saved operator
   route, a separate desktop contract and the only layer that knows it. Neither
   layer invents the other's fact.
4. **Existing argv is not re-split.** The interactive CLI wizard executes an
   action's argv directly, so removing the SSH wrapper from
   `github_trust_argv` while leaving the runner alone would run a remote command
   on the wrong machine. The bundling the human actually read is the desktop's
   own `ssh <target> …` line, which the desktop owns and now renders as a
   separate, separately copyable entry line. Splitting server-side argv is
   therefore out of scope; the execution context supplies the missing frame
   without moving any execution.
5. **A human stop renders as an ordered list of single actions.** One numbered
   step per action. `purpose` and `expected_success` move behind one explicit
   disclosure; `performed_by` is dropped from the body, because the card
   already says a human is required. This follows
   [interface and visual design](../specs/interface-and-visual-design.md): no
   muted commentary line under a heading, and a read-only inspector rather than
   a caption when there is more to say.

   The mockup the human approved was drawn for the deploy-key stop alone, and
   three of its affordances need to know which stop is being drawn: a name for
   each numbered step, which carried values are pasted into GitHub rather than
   only compared, and *Allow write access* as its own requirement. A shared
   panel may not hold that knowledge, so the step declares it instead: an
   action may carry a `title` and a `requirement`, and a value may carry a
   `role` of `input` or `evidence`. All three are optional on the same terms as
   the execution context, and a stop that says nothing renders as it did
   before. Chosen by the human on 2026-09-19 over accepting a generic panel.

6. **The operator stop keeps its own title.** `_copy_operator_contract` takes
   `title` and `purpose` from the operator step, not the pending plan step, so
   the card is named after the human's task. Two independent event validators
   currently refuse that — `validate_event_sequence` in `server_ops/models.py`
   and the desktop's own check in `web/src-tauri/src/server_commands.rs` — and
   both must relax to allow `title` and `purpose` to change on a human
   `operator_action_needed` event, in the same change. Target, phase, expected
   success, ordering, and the responsibility transfer stay pinned, including
   `_copy_operator_contract`'s typed-target check. Because the pause is
   persisted before it is emitted, landing the title change without both
   validators would store the right stop and report a CLI failure.

## Plan

Four slices. The first is the shared contract and lands alone.

1. **Contract.** Add `ExecutionContext` and hang it off `CommandAction` and off
   `ServerStep` as `resume_execution`, optional per decision 2, with the existing
   `_StrictModel` validation and credential-shaped-flag refusals unchanged.
   State the context at every operator stop RCP builds: the four Git operator
   builders in `git_credentials.py`, the retained-checkout stop in
   `project_checkout.py`, the provisioning pause and copy helpers in
   `project_provision.py`, the provider actions and resume in
   `provider_readiness.py`, both member-removal stops in `members.py`, and
   restore preparation in `restore.py`. Mirror the model in `web/src/types.ts`.
   Confirm that a stored pause written before this change still decodes, and
   that a receipt minted before it still matches on retry; if the added key
   moves the transition digest, keep the historical serialization rather than
   weakening the comparison. The separately versioned supervisor is not changed:
   its steps simply carry no context.
2. **Operator stop titles.** Relax both event validators to allow `title` and
   `purpose` to change on a human `operator_action_needed` event, take both from
   the operator step in `_copy_operator_contract`, and title the deploy-key grant
   step and its restore twin for the human's task. The restore twin's new title
   is inert: `restore.py` forwards only actions, fields, and diagnostic, and its
   reply has no title to carry, so nothing propagates it. Giving that reply one
   is a separate contract for a surface this work never touched, and was not
   done here.
3. **Web panel.** Rebuild `OperatorAction` as the ordered list: numbered steps,
   copy controls on every command and on every carried value, the execution
   label with the entry line composed from the saved operator route, and the
   resume command as the final step followed by Refresh. Per decision 5 the
   stop declares what the panel cannot know: each action's name and its one
   requirement, and whether a value is pasted into a form or only compared, so
   the deploy-key grant draws as two inputs with the fingerprint beside them as
   evidence. The saved route is offered as the way into a shell only when it
   lands in that shell and has been proved to run these commands.
   Replace the `.operator-action-line` grid with full-width blocks, keeping the
   two-column grid only for labelled rows. `TransferProjectSetup` renders no
   actions, fields, or resume command at all today; it reuses the same panel,
   and probes its selected connection so the panel can offer that route.
4. **CLI wizard.** Render the execution context in `_render_actions` and in the
   `Continue:` block, in the same words the panel uses, and reserve silence for
   a stored step that never declared one. A declared operator login is still
   named: the wizard has been reaching the server on the operator's behalf, so
   an unlabelled command reads as one more thing RCP already handled. The
   wizard's own execution is unchanged because no argv moves; its
   resume-deduplication now compares argv and context.

## Verification

- `uv run pytest -n0` over `server_ops`, storage, and API projection tests,
  including the step-contract validation tests, the machine-readable round-trip
  fixture, and the supervisor event tests whose unlabelled terminal output
  deliberately changes.
- A test that every operator stop RCP builds carries an execution context, so
  decision 2's optionality cannot decay into an unnoticed omission.
- A stored legacy pause, written without the field, decodes, reloads, and
  retries against its original receipt.
- `node --experimental-strip-types --test web/tests/<affected>.test.mjs` and
  `npm --prefix web run build`; rebuild Tauri and rerun the affected
  `docs/desktop.md` checks, including the inline native event records.
- The served-app journey: drive a provisioning request to the deploy-key stop on
  a throwaway server with a disposable data directory, then read and click the
  rendered panel in a browser — the copy controls, the disclosure, and Refresh.
  The human's own server and data directory are never used.
- The interactive CLI wizard's rendering of the same stop, captured from a real
  terminal run, including Enter-driven continuation rather than rendering alone.

## Open

The design review's open question — whether the resume command should become an
action carrying its own context — is answered in decision 1: it stays
`resume_argv` with a sibling `resume_execution`.

One gap is known and not closed here. `operator_argv` on the provisioning
projection, which the panel's **Copy server command** button copies, is a bare
wrapper invocation with no context of its own. It is a projection field rather
than a step, so giving it one is a separate contract change; until then that
button still hands over a command without saying where it runs.

One unrelated defect was found and left alone: the native test record in
[`server_commands.rs`](../../web/src-tauri/src/server_commands.rs) builds a
`kind: "command"` action carrying `instruction` instead of `argv`, which is
malformed and predates this work. The restore reply's missing title is recorded
with slice 2, which owns it.
