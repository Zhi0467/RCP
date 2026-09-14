# Episodes settle honestly, logins stay alive, reauthorization continues the work

Date: 2026-09-14
Status: design confirmed by the human on 2026-09-14 after a forensic read of the
production database, then revised the same day after an xhigh design review and
two further protocol spikes. Nothing is implemented. Every decision below is
settled. All six slices land on one branch and one pull request as ordered
commits; slices 1, 2, 5, and 6 start first, slices 3 and 4 follow on this same
branch. None is optional.

Close this handoff when every closure criterion at the end holds on the team
server and on the desktop, and the four decision records this handoff cites are
the current authority for the behavior they describe.

## The incident, from the record

Auto-research episode `6f79bce0` on the team server, ceiling 10. All times UTC.
These facts come from a copy of the production database and the production
service journal; the code claims below were reproduced by running the current
source against that copy.

1. Sep 9 23:50 to Sep 10 16:52. Orchestrator turn, two Work workers, two child
   Experiment loops, six root wakes. Ten paid turns were admitted; six of them
   were wakes. Everything ran on Codex under one shared login.
2. Sep 10 16:52 to Sep 12 00:19. No RCP turn started a provider process. The
   RCP service restarted once (Sep 11 06:05) and, as at every start, ran a
   30-second `codex app-server` probe to list skills.
3. Sep 12 00:19. The first Codex process in 31 hours died with
   `refresh_token_reused`. Every Codex process after it died the same way. The
   child Experiment's failure notice woke the orchestrator into the same dead
   login. Automatic exact recovery retried three times over fourteen minutes,
   then marked itself exhausted. Two manual Retry clicks at 04:40 died the same
   way. The human signed in again at 04:55 and retried at 19:49; that turn ran.
4. Sep 12 19:58 to 20:05. The tenth paid turn ran and applied r34. A child
   completed and its report landed. The reconciler wrote `ending=exhausted`,
   `status=wrapping_up`.
5. The wrap-up never started. `auto_research_wrapup_spec` raises "the compact
   Auto-research ending receipt exceeds its storage boundary": the receipt was
   about 8.6 KB after its one built-in trim, over `AGENT_TASK_RECEIPT_MAX_BYTES`
   (8192), because two lifecycle notices carried 800-character provider error
   payloads. `reconcile_auto_research_wrapup` caught the exception, logged a
   warning, returned False, and repeated the same failure on every poll. The
   failure was never written on the episode.
6. The API serialized that episode as `health=active`,
   `recommendation=continue`, `can_reauthorize=false`. The card read "Active,
   Let auto-research continue". Reauthorize requires a state the episode could
   not reach. The stuck episode also counted as the project's one live
   Auto-research episode, so no new episode could start, and the human was told
   the branch could not be merged. A child Experiment with three finished Slurm
   jobs sat in "Needs action" because an ended parent cannot consume pending
   watcher completions.

What consumed the refresh token between Sep 10 16:52 and Sep 12 00:19 is not
proven. No RCP turn ran, nobody used the UI, and no `codex login` happened; the
only RCP-started Codex process in the window was the startup skill probe. The
mechanism is settled regardless: Codex and Claude Code both rotate a single-use
refresh token and neither coordinates across processes. OpenAI closed the report
of this race as not planned; Anthropic has seven open duplicates for Claude Code.
RCP starts a fresh provider process per turn and per probe.

## What is wrong today, by owner

- `src/rcp/runs/auto_research.py::auto_research_wrapup_spec` raises for size
  instead of compacting further, measures size with a different encoder than
  the stored receipt uses, and rebuilds the receipt from live reads on every
  attempt. `src/rcp/runs/episodes/reconcile.py::reconcile_auto_research_wrapup`
  swallows every exception. The episode stays `wrapping_up` with nothing written
  on it.
- `src/rcp/api/episodes.py::_episode_projection` derives health from one task
  pointer and falls through to `active`/`continue` for `wrapping_up` with
  `wrapup_state == "not_started"`. It has no branch for a failed wrap-up, though
  the API and the cards already transport and render `wrapup_error`.
- `src/rcp/providers.py::CodexProfile.credential_failure` does not match the
  text Codex printed (`refresh_token_reused`, "refresh token was already
  used"). `ClaudeProfile` declares no signatures. Experiment loops already turn
  a classified `provider_auth` into a "sign in again" recommendation; Auto-research
  has no such branch, and `src/rcp/runs/auto_research_recovery.py::_recoverable_failure`
  never reads `failure_kind`. The hidden report task and the readiness probe
  have no typed login outcome at all.
- The project attention count sums proposals, decisions, and blockers only. A
  dead login kills every project on a machine and is announced nowhere.
- `src/rcp/provider_skills.py` starts a `codex app-server` at every server
  start; the model catalog probe starts another process on demand. Each is one
  more process that can rotate the shared credential.
- `src/rcp/api/episode_routes.py::reauthorize_episode` accepts only an
  `exhausted`, `needs_action`, settled Auto-research episode and starts a new
  episode on a new branch from `main`, discarding the branch and the session.
  Experiment loops re-run through the node Run route.
- `src/rcp/api/episode_branches.py::graph_branch_summary` requires an ending
  or a paused orchestrator and quiescence before a branch may merge, and
  `src/rcp/runs/branch_merge.py` models a non-null `episode_ending`, so an exact
  head with no writer cannot be merged while its episode is stuck.
- Root wakes spend a paid turn for mail that arrived mid-turn (the inbox
  harvest returns lifecycle notices only), for the orchestrator's own Stop
  settling, for its own replacement advancing, and for a child's login failure.
  Stop and replacement mutations do not record who initiated them.
- The Runs card renders Turns and Mail as two independently sorted lists. Task
  lineage and continuation causes exist per task, but there is no typed causal
  timeline.

## Decisions settled on 2026-09-14

1. **The ending receipt compacts to its bound and never fails the wrap-up.** It
   is built once per ending from one coherent snapshot, measured with the same
   encoder and envelope that storage uses, persisted at admission, and reused on
   every later attempt.
2. **A reconciler failure is durable episode state, phase-specific, recorded
   once.** A permanent admission defect settles the episode with a visible
   wrap-up error; a login blockage parks the wrap-up until sign-in; a transient
   unavailability is retried with bounded backoff and recorded as a diagnostic
   after it repeats. See
   [reconciler failures are durable state](../decisions/2026-09-14-reconciler-failures-are-durable-state.md).
3. **An episode with an ending is never `active`.** Health comes from an
   exhaustive state table over ending, wrap-up state, recovery, failure kind,
   and live tasks. A `blocked_reason` field names the one human action that
   clears a block: `sign_in` or `reauthorize`. A report failure stays a
   nonblocking report error, as the current specs already say; it never changes
   health on its own.
4. **Reauthorization continues the work on the same branch and in the same
   session, as a continuation episode.** A new episode record chained to the
   ended one by `continues_episode_id`, on the same graph branch, with the
   orchestrator resumed in its exact native session and told how many turns it
   has. The ended episode stays terminal history with its report. Same shape
   for Experiment loops. Offered on any episode with a terminal status and no
   live turn, including a human-stopped one; stopped watchers stay stopped. See
   [reauthorization continues on the same branch](../decisions/2026-09-14-reauthorization-continues-on-the-same-branch.md).
5. **A branch merges on branch facts.** Exact head newer than its base, no
   successful receipt for that head, no queued, running, or pausing
   graph-capable writer on the branch. Nothing about the episode is a condition
   and merging ends nothing. "End and merge to main" goes away. See
   [a branch merges on branch facts](../decisions/2026-09-14-a-branch-merges-on-branch-facts.md).
6. **Provider logins are kept alive by giving every credential one refresh path
   and starting no needless process.** Codex stays one process per turn; the
   shared owner process is deferred to its own design track because the
   installed protocol cannot carry Work's per-thread permission profile and
   command authority is proven by process ancestry today. Claude runs on a
   one-year static `setup-token` injected by RCP, so it never rotates anything.
   See [provider logins are kept alive](../decisions/2026-09-14-provider-logins-are-kept-alive.md).
7. **Sign-in happens in the RCP UI.** Codex through `codex login --device-auth`
   run by RCP as the execution account, its user code and URL shown in the UI;
   Claude by pasting the setup token. Any team member may do either; the action
   is attributed to that member and visible to every member. This is a narrow,
   documented exception to the rule that machine credentials are never
   configured from a member session.
8. **A dead login stops new launches before any budget is spent and is
   announced once, everywhere.** Classification covers every path a login
   failure can take. No automatic retry of a login failure. Blocked work resumes
   after a verified sign-in, each blocked allocation claimed once.
9. **Wake policy with durable provenance.** A running orchestrator turn reads
   its mail. A Stop or replacement the orchestrator itself requested, and a
   child failure classified as a login failure, never spend a wake; the notice
   records why it did not wake anyone instead of pretending it was read. The
   coalescing window stays at five seconds.
10. **Timeline.** One time axis with events branching off it, built from
    persisted causal facts written by the earlier slices. The backend emits
    typed events; the web holds one timeline model class and a render
    configuration. It replaces the Turns and Mail sections for both modes.
11. **No periodic logging.** RCP verifies how its own warnings reach the
    service journal before claiming anything about that route; repeating
    failures are durable state (decision 2) and are logged on the first durable
    transition only.

## Design

### Episode lifecycle

An ending fence writes `ending` and `status=wrapping_up`. Settlement admits the
hidden report once. The receipt is built once, after the episode is quiescent
so its reads are coherent, in a deterministic order with explicit tie-breakers,
and it compacts in a fixed order until it fits: lifecycle payload text 800 to 240 to 80 characters, then drop oldest
facts; child diagnostics 480 to 160, then drop oldest; list lengths 16 to 8 to 4
to 0 for command facts, graph results, actors, and child work; starting
instruction 1200 to 480 to 160; finally the meter and counts alone. Every count
field stays honest. Size is measured on the complete stored envelope, mode,
ending, episode id, partial flag, and diagnostic included, with the same
encoder `compact_episode_receipt` uses. The persisted receipt is the fence;
once a wrap-up row exists the reconciler reuses it and never rebuilds the spec.

Failure policy by phase:

- **Permanent admission defect** (validation, malformed ledger, no session to
  report from): the wrap-up is marked `failed` with the exception text as
  `wrapup_error` through the existing failed-wrap-up path, the episode settles to
  its ending's terminal status, and the reconciler stops. The card shows the
  error as a nonblocking report error. Reauthorization and merge remain
  available.
- **Login blockage** on the report's provider: the wrap-up stays `pending` with
  `blocked_reason=sign_in`; it resumes after a verified sign-in and spends no
  report attempt.
- **Transient unavailability** (database lock, canonical repository lock, SSH
  to the stage host, any exception not classified as permanent): retried on the
  next poll; the diagnostic receipt on the reconciling operation stays as today;
  it never becomes terminal on its own.

One warning line is logged per process for each episode and failure kind and
none on repeats. The journal route was inspected: the service configures no
root handler, so Python's last-resort handler writes warnings to stderr and
systemd stores them at its default `info` priority, which is why a
`--priority=warning` filter showed nothing. No logging change is needed.

Health table. Rows are evaluated top to bottom; the first match wins. "Live
turn" means a queued, running, or pausing visible task of the episode. "Settled"
means the episode status is terminal (`completed`, `failed`, `needs_action`,
`stopped`); `not_started` on a settled episode means the ending had no report
to generate, as `end_episode_without_report` records it.

| Ending | Wrap-up | Other facts | Health | Recommendation | blocked_reason |
| --- | --- | --- | --- | --- | --- |
| `stopped` | any | | `stopped` | `none` | |
| any | `pending`, `running` | report blocked on login (slice 2) | `wrapping_up` | `wait` | `sign_in` |
| any | `pending`, `running`, or `not_started` while status is `wrapping_up` | | `wrapping_up` | `wait` | |
| `completed` | `ready` | | `completed` | `open_report` | |
| `completed` | `failed`, `skipped`, `legacy_unavailable`, settled `not_started` | | `completed` | `none` (report error shown when failed) | |
| `failed` | `ready` | | `failed` | `open_report` | |
| `failed` | `failed`, `skipped`, `legacy_unavailable`, settled `not_started` | | `failed` | `review` (report error shown when failed) | |
| `exhausted`, `human_pause` | `ready` | | `needs_action` | `reauthorize` | `reauthorize` |
| `exhausted`, `human_pause` | `failed`, `skipped`, `legacy_unavailable`, settled `not_started` | | `needs_action` | `reauthorize` (report error shown when failed) | `reauthorize` |
| none | | Stop requested, turn finishing | `stopping` | `wait` | |
| none | | recovery pending | `recovering` | `wait` | |
| none | | control task failed with `failure_kind=provider_auth` | `needs_action` | the recovery control | `sign_in` |
| none | | control task paused, interrupted, or failed | `needs_action` | the recovery control, else `review` | |
| none | | queued turn | `starting` | `wait` | |
| none | | live turn | `active` | `wait` or `pause` | |
| none | | no live turn, waiting on watchers, children, or mail | `active` | `continue` | |

No row yields `active` when `ending` is set. The same table drives the Runs card,
the Experiment control, and web automation; the browser derives nothing.

### Continuation episode

`POST /api/projects/{project_id}/episodes/{episode_id}/continue` with
`{"invocation_ceiling": N, "request_id": <client uuid>}` for both modes. It
refuses while the source episode has a live turn, while a merge task is running
on its branch, and while a newer live episode already occupies the project (Auto)
or the node (Experiment). In one transaction it creates the continuation episode
with a fresh id, `continues_episode_id` set to the source, the source's graph
target and `graph_base_head`, ceiling N, its own authorizer snapshot, and
`status=running`; carries the source's child routes and their pending watcher
completions over to the continuation; records a `reauthorized` lifecycle notice
naming N; and admits the first turn as the orchestrator's `thread/resume` of the
source's exact native session, which spends invocation 1 of N. Experiment loops
continue the same node with the same session through the existing Run path,
now marked as a continuation. The source episode is untouched: its ending,
receipt, report, attempts, watchers, and notices stay exactly as they were, and
`episodes_one_live_*` uniqueness holds because the source is terminal. E for the
continuation is 5N. The same `request_id` twice returns the same continuation.

Stopped watchers of the source stay stopped. Child routes the source's ending
froze are carried over with their pending completions, which become deliverable
because the continuation is running. Another member may continue an episode they
did not authorize; the continuation records that member, the source keeps its
own authorizer, and both are shown.

The Runs card and the Experiment board show one control, "Add N turns", on any
episode whose status is terminal and which has no live turn and is not already
continued. The card shows a continuation chain as one run with its ceilings and
endings in sequence; the timeline stitches them.

### Branch merge

`graph_branch_summary`, `branch_merge_admission`, the merge worker's recheck,
and `BranchMergeRequest` all drop the episode conditions. Eligibility is: exact
head newer than base, no successful receipt for that head, no queued, running,
or pausing graph-capable task on the branch. The reciprocal fence that blocks new
graph work on a branch while its merge runs stays. `auto_research_can_end_for_merge`
and the paused-episode settlement it drives are retired, and the "End and merge
to main" control with them. Historical episodes ended by that path keep their
records.

### Provider logins

Codex stays one process per turn, launched through the existing gate. The
hardening is:

- The startup skill inventory refresh runs only when the configured binary or
  its version changed since the stored inventory, and only through the
  credential gate; the model catalog probe is cached the same way. Neither ever
  runs concurrently with a turn start.
- A probe or turn is never terminated before the gate's minimum hold has
  elapsed, so a refresh started at process start can finish its write.
- The shared owner process is deferred: the installed protocol ignores a
  per-thread named permission profile (spike, below), and operational command
  authority is proven by invocation process ancestry
  (`src/rcp/agents/invocation_broker.py`, `staged_command_broker.py`). Its
  design track needs per-thread permissions from the protocol, a per-turn
  command authority mechanism, remote attachment, and interrupt semantics, and
  is not part of this handoff.

Claude runs on a static token: `claude setup-token` runs on any machine with a
browser, the human pastes the token into the RCP UI, RCP stores it in the
execution account's credential store as a 0600 file under the data directory
(the macOS Keychain CLI path truncates values of this length) with the paste time
and member, and injects it as `CLAUDE_CODE_OAUTH_TOKEN`. One per-provider,
per-execution-account environment builder supplies that variable to every Claude
process RCP starts: turns, hidden reports, readiness, skill probes, doctor and
update checks, and the remote login shell on SSH machines. Conflicting inherited
sources (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `apiKeyHelper`) are removed
from that environment so the token is the credential in use. Expiry is unknown
from the token itself; RCP shows the paste date, warns eleven months after it as
an estimate, and treats a classified login failure as the truth. The credential
gate keeps covering Claude for the probes that still read its own login.

Sign-in from the UI: for Codex, RCP runs `codex login --device-auth` as the
execution account, parses the user code and verification URL from its output,
shows both, and reports the exit; then verifies the login with one minimal real
request before clearing any blockage. For Claude, the token paste is verified the
same way. The state (`signed_in`, `signed_out`, `verifying`) and the last
sign-in's member are shown in Settings and by `rcp server doctor`. The doctor
stops reporting `codex login status` as proof of a login.

### Login failure handling

One typed outcome, `provider_auth`, propagated through every owner:

| Path | Owner | Handling |
| --- | --- | --- |
| turn, worker, wake, recovery child | `background.py` finalizer via `classify_agent_failure` | signatures added; kind stored before settlement creates notices |
| readiness before launch | `agents/launcher.py` | typed auth outcome from readiness, not generic text |
| skill, catalog, readiness probes | `provider_skills.py`, `server_ops/provider_readiness.py` | typed probe outcome persisted on the machine-account state |
| hidden report attempt | `runs/tasks/episode_report.py` | classified before its retry loop; blocked auth spends no attempt and parks the wrap-up |
| final report failure | `storage/episodes.py` | kind persisted in the same transaction |
| Auto recovery: scheduling, due rows, restart | `runs/auto_research_recovery.py` | guard scheduling and claiming, including pre-existing pending rows |
| Experiment Stop recovery | `runs/experiment_recovery.py` | graceful Stop preserved; the exact retry waits for sign-in |
| SSH transport retry | `background.py` | already limited to `transport_lost`; unchanged |

Codex signatures: `refresh_token_reused`, "refresh token was already used", plus
the existing four. Claude signatures are added only from observed text; the first
sample is recorded in the profile comment.

The machine-account state records `signed_out` with a credential generation when
a `provider_auth` outcome lands. Admission of any paid turn, child Work, child
Experiment, wake, or report attempt for that provider on that machine is refused
before task creation and budget debit while the state is `signed_out`; the
refusal is recorded on the episode as `blocked_reason=sign_in`. A late failure
carrying an older generation does not re-mark a repaired account. After a
verified sign-in RCP claims each blocked allocation once, rechecks Stop,
membership, ending, and target, and resumes it through its exact recovery path;
nothing on other providers or machines is touched. One notice appears on every
project's Runs view and in the project list while any machine account this
project uses is `signed_out`.

### Wake policy

Stop and replacement mutations record `initiated_by` (human member or
orchestrator operation id) and the effect id in the same transaction. A
lifecycle notice for a stop the orchestrator requested, or for a replacement it
created advancing, is created with `wake_suppressed="self_caused"`; a child task
failure with `failure_kind=provider_auth` is created with
`wake_suppressed="provider_auth"`. Suppressed notices never admit a wake and
never block ordinary mail delivery; the inbox harvest returns them so the
orchestrator still learns of them. Legacy notices with no provenance are
delivered as today. A failed replacement is new information and does wake.

`process_auto_research_lifecycle_inbox` snapshots and consumes eligible notices
and root-addressed mail in one `BEGIN IMMEDIATE` transaction, records the
selected ids and the bounded reply in its idempotent effect receipt, and
attributes consumption to the running turn. Mail arriving after a turn's last
harvest stays pending for a later wake.

### Timeline

`GET /api/projects/{project_id}/episodes/{episode_id}/timeline` returns typed
events in time order across a continuation chain: `turn` (role, cause, revision,
status, attempt), `retry` (parent turn), `wake` (cause and the notice or message
ids it delivered), `mail`, `child` (episode id, control node, event), `lifecycle`
(ending fenced, ceiling reached, recovery exhausted, wrap-up failed, continued,
signed out, sign-in), and `human` (stop, retry, continue, message, with the
member). Each event carries `at`, `actor`, `parent_event_id`, and the task,
message, or episode it links to. Facts the earlier slices persist are the source;
history without provenance is labelled unknown, not guessed. The web holds one
`EpisodeTimeline` model class and a `TimelineRenderConfig` mapping event kinds to
lane, glyph, color token, and fold behavior. Mail is folded and opens on click; a
task event opens the task inspector; a child event opens the child episode. It
replaces the Turns and Mail sections in the Runs card for both modes.

## Slices

All six land on this branch and pull request as ordered commits, implemented
with Codex (gpt-6-astra, medium), reviewed by Claude and by the Codex GitHub
reviewer after each push, verified as listed, and merged by the human once.
Order: 1, 2, 5, 6, then 3, 4.

### Slice 1: settle and honest state

Owners: `src/rcp/runs/auto_research.py`, `src/rcp/runs/episodes/reconcile.py`,
`src/rcp/runs/episodes/wrapup.py`, `src/rcp/storage/episodes.py`,
`src/rcp/storage/models.py` (`blocked_reason`), `src/rcp/api/episodes.py`,
`src/rcp/api/experiment_controls.py` (consumes the same table), `web/src/types.ts`,
`web/src/campaigns.ts`, `web/src/runProjection.ts`, `web/src/components/CampaignRuns.tsx`,
`web/src/components/ExperimentRunDetail.tsx`.
Invariants: canonical Patch logs untouched; one transition per mutation; health
literals unchanged; report failure nonblocking.
Checks: compaction tests with oversized payloads, Unicode, and equal
timestamps; a persisted-receipt reuse test across two reconcile passes; a test
per failure phase; a serialization test per row of the health table, including
the production shape before and after repair; a replay of the copied production
database through the new code showing the stuck episode's receipt compact under
the bound and its wrap-up admit, and a forced permanent defect settle it to
`needs_action` with a visible wrap-up error and `blocked_reason=reauthorize`. `uv run pytest -n0` on the
touched test files, `uv run ruff check`, `npm --prefix web run build`.

### Slice 2: login failure handling

Owners: `src/rcp/providers.py`, `src/rcp/agents/failure_kinds.py`,
`src/rcp/agents/launcher.py` (typed readiness outcome), `src/rcp/background.py`,
`src/rcp/runs/tasks/episode_report.py`, `src/rcp/runs/auto_research_recovery.py`,
`src/rcp/runs/experiment_recovery.py`, `src/rcp/storage/auto_research.py` and
`src/rcp/storage/auto_research_children.py` (admission refusal before debit),
`src/rcp/storage/experiments.py`, new machine-account state in `src/rcp/storage/`
and its API in `src/rcp/api/`, `src/rcp/api/episodes.py`, `src/rcp/api/index.py`,
`src/rcp/projects.py`, `web/src/views/*Runs*`, `web/src/components/AttentionRail.tsx`.
Invariants: agent capability unchanged; classification from the provider's own
text only; exact recovery spends no budget.
Checks: signature tests with the recorded production text; the failure-path
matrix as tests, one per row; admission refusal before debit for every paid
admission path; a late old-generation failure not re-marking a repaired
account; a served-app check that the notice appears on every project.

### Slice 3: provider login hardening and sign-in from the UI

Owners: `src/rcp/provider_skills.py`, `src/rcp/agents/credential_gate.py`,
`src/rcp/agents/launcher.py` (environment builder, no early termination),
new `src/rcp/agents/provider_environment.py`, new `src/rcp/api/provider_login.py`,
`src/rcp/server_ops/provider_readiness.py`, `src/rcp/server_ops/provider_update.py`,
`src/rcp/server_ops/doctor.py`, `src/rcp/server_ops/backup_models.py` (the
token file is classified in the backup inventory as excluded and re-pasted after
restore), `web/src/views/Settings*`, `web/src/components/AgentConfigControls.tsx`.
Invariants: 4 (capability fixed; the environment carries a credential, never a
capability); secrets never appear in argv, events, receipts, or logs.
Checks: a fake provider proving every Claude launch path receives the token and
no conflicting variable; the probe never running during a turn start and never
being terminated inside the gate hold; a served-app device-code sign-in on the
team server completing with no shell; doctor reporting state from a real
request.

### Slice 4: continuation episode and branch merge on branch facts

Owners: `src/rcp/api/episode_routes.py`, `src/rcp/api/episodes.py`,
`src/rcp/api/episode_branches.py`, `src/rcp/api/experiments.py`,
`src/rcp/storage/episodes.py`, `src/rcp/storage/base.py` (`continues_episode_id`,
`request_id`), `src/rcp/storage/auto_research.py`,
`src/rcp/storage/auto_research_children.py` (route carry-over),
`src/rcp/storage/experiments.py`, `src/rcp/storage/agent_tasks.py` (merge
gates), `src/rcp/runs/auto_research_admission.py`, `src/rcp/runs/auto_research_delivery.py`,
`src/rcp/runs/branch_merge_admission.py`, `src/rcp/runs/tasks/branch_merge.py`,
`src/rcp/runs/branch_merge.py`, `src/rcp/runs/experiment_loop.py`,
`src/rcp/transfer/records.py` and `src/rcp/storage/transfer.py` (the chain
field), `src/rcp/agents/auto_research_prompt.py`,
`src/rcp/agents/experiment_loop_prompt.py`, `web/src/App.tsx`, `web/src/api.ts`,
`web/src/components/CampaignRuns.tsx`, `web/src/components/ExperimentRunDetail.tsx`.
Invariants: 3 (a continuation is a new human grant); 6b; 10g (the continuation
resumes the exact session or refuses); the source episode is never mutated; the
branch is never recreated.
Checks: a storage test for the atomic continuation; idempotent `request_id`; a
frozen child completion delivered to the continuation; refusal while a merge
runs or a live successor exists; eligibility tests that an exact head merges
while its episode is `running`, `wrapping_up`, or ended and that a live writer
is refused with its name; prompt data tests for the `reauthorized` notice; a
served-app check continuing the copied production episode and merging its
branch first.

### Slice 5: wake provenance and mail harvest

Owners: `src/rcp/storage/auto_research_children.py`, `src/rcp/storage/auto_research.py`,
`src/rcp/storage/base.py` (`initiated_by`, `wake_suppressed`),
`src/rcp/runs/auto_research_experiments.py`, `src/rcp/runs/auto_research_effects.py`,
`src/rcp/runs/auto_research_delivery.py`, `src/rcp/agents/auto_research_prompt.py`.
Invariants: every paid wake still spends one unit; no notice is marked read
that no turn read.
Checks: tests that harvest returns and consumes mail and notices in one
transaction with an idempotent receipt; that a self-requested stop or
replacement admits no wake and a failed replacement does; that a
`provider_auth` child failure admits no wake and does not block mail; prompt
data tests.

### Slice 6: timeline

Owners: new `src/rcp/api/episode_timeline.py`, read queries in
`src/rcp/storage/auto_research.py` and `src/rcp/storage/episodes.py`, new
`web/src/timeline.ts`, new `web/src/components/EpisodeTimeline.tsx`,
`web/src/components/CampaignRuns.tsx`, `web/src/components/ExperimentRunDetail.tsx`,
`web/src/styles.css`.
Invariants: read-only projection; no new authority.
Checks: API tests building the timeline of the production copy, asserting six
wakes carry their causes, five retries nest under their turns, and unknown
provenance is labelled; `node --experimental-strip-types --test
web/tests/timeline.test.mjs`; a served-app render with a screenshot in the PR.

## Documents and prompts this contract changes

Reversed rules, each carried by a decision record: `docs/server.md` lines 253 to
321 (RCP does not log in to providers; never paste a token into RCP);
`docs/specs/server-and-machine-operations.md` lines 57 to 63 (RCP does not
manage provider authentication); `docs/specs/authority-and-proposals.md` lines
10 to 22 (machine credentials never configured from a member session; the
provider sign-in exception); `docs/specs/auto-research-and-branch-merge.md`
(reauthorization always creates a new branch; merge eligibility tied to the
episode).

Specs: `providers-and-containment.md` (probe policy, gate hold, environment
builder, signatures, the deferred owner track), `auto-research-and-branch-merge.md`
(continuation episode, wake provenance, mail harvest, settle with a visible
wrap-up error, merge on branch facts), `conversations-episodes-and-watchers.md`
(Experiment-loop continuation, report error stays nonblocking),
`api-web-and-desktop-projections.md` (health table, `blocked_reason`, timeline,
sign-in state), `interface-and-visual-design.md` (timeline),
`projects-spaces-and-operations.md` (who signs in, token record, backup
inventory), `paper-artifacts-and-result-views.md` (unchanged rule, cited),
`design.md` (provider login hardening; the persistent-daemon sentence stays true),
`README.md` and `docs/desktop.md` (sign-in through the UI).

Prompts and skills: `src/rcp/agents/auto_research_prompt.py` (harvest includes
mail; suppressed notices; the `reauthorized` notice), `src/rcp/agents/experiment_loop_prompt.py`
(`human_reauthorization` means a continuation of the same session with more
turns), `src/rcp/skills/episode-report/SKILL.md` (a continuation has its own
report). Tests assert prompt data and enforcement, never wording.

## Spikes already run

On codex 0.154.0, in a logged-out scratch `CODEX_HOME`, 2026-09-14:

- One `codex app-server` over stdio accepted two `thread/start` calls with
  different `cwd` and sandbox and echoed each per thread; two `turn/start` calls
  ran concurrently with every notification carrying its `threadId`.
- `--listen unix://` answers a WebSocket upgrade and closes a newline-JSON
  client.
- A per-thread named permission profile supplied through thread `config` is
  ignored: the response carries `permissions: null` and a sandbox derived from
  the cwd alone. This is why the shared owner is deferred.
- `account/login/start` accepts `type=chatgptDeviceCode` and returns
  `userCode`, `verificationUrl`, `loginId`; `account/login/completed` reports
  the result. Retained for the deferred track; slice 3 uses `codex login
  --device-auth` instead.

## Closure criteria

- The stuck production episode, replayed from a copy through slice 1, compacts
  its receipt under the bound and admits its wrap-up; a forced permanent defect
  settles it to `needs_action` with a visible wrap-up error and
  `blocked_reason=reauthorize`; in neither state does its card say active.
- A recorded login failure classifies as `provider_auth` on every row of the
  failure matrix, is never retried automatically, is refused before budget on
  every admission path, and is announced on every project.
- On the team server: the skill and catalog probes start no process at service
  start when the binary is unchanged; sign-in completes from the RCP UI by device
  code with no shell; `rcp server doctor` reports login state from a real
  request; Claude turns run under the pasted token and never write
  `.credentials.json`.
- "Add N turns" creates a continuation of the copied production episode on its
  own branch in its own session, the frozen child completion is delivered to it,
  and the source episode is byte-for-byte unchanged.
- The copied production branch merges to main while its episode is
  `wrapping_up`, and a branch with a running writer is refused with the writer
  named.
- Mail arriving mid-turn is read by that turn and causes no wake; a
  self-requested stop or replacement causes no wake; a child login failure
  causes no wake and does not block mail.
- The Runs card shows the timeline for the production copy with six wakes
  labelled by cause and five retries nested under their turns, and the Turns and
  Mail sections are gone.
- Every document and prompt listed above says what the code does.
