# Episodes settle honestly, logins stay alive, reauthorization continues the work

Date: 2026-09-14
Status: design confirmed by the human on 2026-09-14 after a forensic read of the
production database; nothing is implemented. The spikes in
[Spikes already run](#spikes-already-run) are done. Every decision below is
settled. The six slices are one contract: none is optional and none is deferred.

Close this handoff when every closure criterion at the end holds on the team
server and on the desktop, and the three decision records this handoff cites are
the current authority for the behavior they describe.

## The incident, from the record

Auto-research episode `6f79bce0` on the team server, ceiling 10. All times UTC.

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
5. The wrap-up never started. Running the current code against a copy of the
   database reproduces it:
   `auto_research_wrapup_spec` raises "the compact Auto-research ending receipt
   exceeds its storage boundary". The receipt was about 8.6 KB after its one
   built-in trim, over `AGENT_TASK_RECEIPT_MAX_BYTES` (8192), because two
   lifecycle notices carried 800-character provider error payloads. The
   reconciler caught the exception, logged a warning nothing captured, returned,
   and repeated the same failure on every poll for two days.
6. The API serialized that episode as `health=active`,
   `recommendation=continue`, `can_reauthorize=false`. The card read "Active,
   Let auto-research continue". Reauthorize was hidden because it requires a
   state the episode could never reach. A child Experiment with three finished
   Slurm jobs sat in "Needs action" because a parent that has ended cannot
   consume pending watcher completions.

What consumed the refresh token between Sep 10 16:52 and Sep 12 00:19 is not
proven. No RCP turn ran, nobody used the UI, and no `codex login` happened. The
only RCP-started Codex process in that window was the startup skill probe. The
mechanism is settled regardless: both Codex and Claude Code rotate a single-use
refresh token on every refresh and neither coordinates across processes. OpenAI
closed the report of this race as not planned and pointed at a single shared
app-server process. Anthropic has seven open duplicates of the same race for
Claude Code. RCP starts a fresh provider process per turn and per probe, so RCP
is the component that manufactures the race.

## What is wrong today, by owner

- `src/rcp/runs/auto_research.py::auto_research_wrapup_spec` raises for size
  instead of compacting further. `src/rcp/runs/episodes/reconcile.py::reconcile_auto_research_wrapup`
  swallows the exception. The episode stays `wrapping_up` forever.
- `src/rcp/api/episodes.py::_episode_projection` derives health from one task
  pointer and falls through to `active`/`continue` for `wrapping_up` with
  `wrapup_state == "not_started"`. It never reports a wrap-up that failed.
- `src/rcp/providers.py::CodexProfile.credential_failure` does not match the
  text Codex actually printed (`refresh_token_reused`, "refresh token was
  already used"). `ClaudeProfile` declares no signatures at all.
- `src/rcp/runs/auto_research_recovery.py::_recoverable_failure` never reads
  the task's `failure_kind`, so a login failure is retried like a dropped link.
- Provider sign-in state is invisible. The project attention count sums
  proposals, decisions, and blockers only. A dead login kills every project on
  the machine and appears nowhere.
- `src/rcp/agents/codex_app_server.py` starts one app-server per turn. `exec`
  starts one process per turn. `src/rcp/provider_skills.py` starts another at
  every server start. Each process reads the same `auth.json` and may rotate the
  token. `src/rcp/agents/credential_gate.py` serializes startup only.
- `src/rcp/api/episode_routes.py::reauthorize_episode` accepts only an
  `exhausted`, `needs_action`, settled Auto-research episode and starts a new
  episode on a new branch. Experiment loops re-run through the node Run route.
- Root wakes spend a paid turn for mail that arrived while the orchestrator was
  running (its inbox harvest returns lifecycle notices only), for the settling of
  its own Stop, for its own replacement advancing, and for a child's login
  failure.
- The Runs card renders Turns and Mail as two independently sorted lists. Wake
  cause, recovery lineage, and child lifecycle are not in the episode API.
- `rcp serve` writes uvicorn access lines to the journal and nothing from RCP's
  own loggers.

## Decisions settled on 2026-09-14

1. **The ending receipt compacts to its bound and never fails the wrap-up.**
2. **A reconciler failure is durable episode state, recorded once, not a log
   line.** See
   [reconciler failures are durable state](../decisions/2026-09-14-reconciler-failures-are-durable-state.md).
3. **An episode with an ending is never `active`.** Health is derived from the
   ending, wrap-up state, recovery state, failure kind, and children. A new
   `blocked_reason` field names why a human is needed: `sign_in`,
   `reauthorize`, `wrapup_failed`, `child_blocked`. The health vocabulary
   otherwise stays as it is.
4. **Reauthorization continues the episode.** Same episode id, same branch,
   same orchestrator session, ceiling raised by the number the human types,
   ending cleared, one lifecycle notice telling the orchestrator how many turns
   it has. The same for Experiment loops. A report already produced is
   superseded by the next ending's report. Children the parent's exhaustion
   froze become deliverable again. See
   [reauthorization continues the episode](../decisions/2026-09-14-reauthorization-continues-the-episode.md).
5. **One credential-holding provider process per machine account.** For Codex,
   RCP owns one long-lived `codex app-server` child per machine account over
   stdio, under an RCP-owned `CODEX_HOME`, and every turn, probe, and skill
   listing is a thread or request inside it. For Claude, RCP holds a one-year
   `claude setup-token` in its credential store and injects it as
   `CLAUDE_CODE_OAUTH_TOKEN`; Claude processes never touch `.credentials.json`.
   See
   [one provider login owner per machine](../decisions/2026-09-14-one-provider-login-owner-per-machine.md).
6. **Sign-in happens in the RCP UI.** Codex through `account/login/start` with
   `chatgptDeviceCode` on the owner process, showing the user code and the
   verification URL; Claude by pasting the setup token. No shell, no `sudo`, no
   login as the service account. Any team member may sign in; the action is
   attributed to that member and visible to every member. RCP invents no admin
   role because it has none.
7. **A dead login stops new launches and is announced once, everywhere.**
   Codex and Claude signatures are those the CLIs actually printed. No automatic
   retry of a login failure. Episodes caught mid-flight pause with
   `blocked_reason=sign_in` and resume on their own after a successful sign-in.
8. **`exec` and the per-turn app-server runtime are retired for Codex.** There is
   no silent fallback to a per-turn process; that would recreate the race. When
   the owner is down RCP refuses launches with the reason and restarts it.
9. **Wake policy.** A running orchestrator turn can read its mail. The
   orchestrator's own Stop settling and its own replacement advancing never
   spend a wake, and neither does a child failure classified as a login failure.
   Other child failures still wake. The coalescing window stays at five seconds.
10. **Timeline.** One time axis with events branching off it: turns, recovery
    attempts nested under the turn they retry, wakes labelled with their cause,
    mail folded and clickable, child episodes as branches, lifecycle facts as
    markers. The backend emits typed events; the web holds one timeline model
    class and a render configuration. It replaces the Turns and Mail sections for
    both episode modes.
11. **No periodic logging.** RCP's own warnings reach the journal through one
    stderr handler at WARNING. Repeating failures are durable state (decision 2)
    and are logged once.
12. **Codex transport is one stdio connection per owner, multiplexed by RCP.**
    The `--listen unix://` listener speaks WebSocket; the desktop Codex app itself
    runs one `codex app-server --listen stdio://`. RCP does the same.

## Design

### Episode lifecycle

An ending fence writes `ending` and `status=wrapping_up`. Settlement then admits
the hidden report exactly once. The receipt builder compacts in fixed order
until the receipt fits: lifecycle payload text 800 to 240 to 80 characters, then
drop oldest facts; child diagnostics 480 to 160, then drop oldest; list lengths
16 to 8 to 4 to 0 for command facts, graph results, actors, and child work;
starting instruction 1200 to 480 to 160; finally the meter and counts alone. The
count fields stay honest at every step. The receipt is deterministic for the same
inputs because the wrap-up fence compares receipts for equality.

If the report cannot be admitted for any reason, the wrap-up is marked `failed`
with the exception text as `wrapup_error` through the existing failed-wrap-up
path, the episode settles to its ending's terminal status (`exhausted` to
`needs_action`), and the reconciler stops attempting it. The card shows the
error. Reauthorize and branch merge remain available; a failed wrap-up is a
missing report, not a lost episode.

Health derivation, in order: stopped, completed, or failed ending with a settled
wrap-up gives that terminal health; an ending with a report pending or running
gives `wrapping_up`; an ending with a failed wrap-up gives `needs_action` with
`blocked_reason=wrapup_failed`; a task failed with `failure_kind=provider_auth`
gives `needs_action` with `blocked_reason=sign_in`; an exhausted ending gives
`needs_action` with `blocked_reason=reauthorize`; a parent waiting on a child that
cannot proceed gives `needs_action` with `blocked_reason=child_blocked`; only an
episode with no ending and a live or queued task is `active` or `starting`. The
recommendation follows the blocked reason, one action per reason.

### Reauthorization

`POST /api/projects/{project_id}/episodes/{episode_id}/reauthorize` with
`{"additional_invocations": N}` for both modes. It refuses while a turn is
queued, running, or pausing. It raises `invocation_ceiling` by N in the same
transaction that clears `ending`, `ending_diagnostic`, and `stop_requested_at`,
sets `status=running`, resets `wrapup_state` to `not_started` while retaining
any existing report row as history, and records a lifecycle notice
`reauthorized` with the new remaining count. Delivery of that notice is the wake
that resumes the orchestrator's native session through `thread/resume`; for an
Experiment loop it is the ordinary next turn. Pending child watcher completions
become deliverable because the parent is running again. The branch is untouched.
Human Stop and merge keep their semantics. The Runs card and the Experiment board
show one control, "Add N turns", on any episode whose health carries
`blocked_reason` `reauthorize` or `wrapup_failed`, or whose ending is
`exhausted`, `completed`, or `failed` with no live turn.

### Provider login owner

Per machine account RCP runs at most one `codex app-server` child over stdio,
`CODEX_HOME` set to `<data dir>/providers/codex/home` on the machine RCP runs on
and `~/.rcp/providers/codex/home` under the SSH user on a remote machine. The
owner is started with the containment config RCP already names per process
(`agents`, `apps`, `mcp_servers`, `plugins`, `hooks`, `notify`, instruction
channels, `shell_environment_policy`), which is identical for every project. Every
per-project fact is per thread: `cwd`, writable roots and sandbox mode, approval
policy, model, effort, base and developer instructions. A turn is `thread/start`
or `thread/resume` followed by `turn/start`; steering is `turn/steer`; usage,
items, and completion arrive as notifications carrying `threadId` and `turnId`.
Skills come from `skills/list`, models from `model/list`, sign-in state from
`account/read`. The startup skill probe and the model catalog probe no longer
start a process.

Supervision: RCP starts the owner lazily on first need, restarts it with bounded
backoff when it exits, and publishes its state (`running`, `starting`,
`stopped`, `signed_out`) per machine account. A turn admitted while the owner is
down is refused with that reason, never rerouted to a per-turn process. On a
remote machine the owner runs under the existing OS process owner
(`systemd_user` on Linux, `launchd` on macOS) and RCP attaches over its
persistent SSH channel; the first live check of slice 3 is exactly that.

Sign-in: the UI calls `account/login/start` with `type=chatgptDeviceCode`, shows
`userCode` and `verificationUrl`, and waits for `account/login/completed`.
`account/logout` signs out. The state and the last sign-in's member are shown in
Settings and in the server doctor.

Claude: `claude setup-token` runs on any machine with a browser. The human pastes
the token into the RCP UI. RCP stores it in the machine's credential store as a
0600 file under the data directory (the macOS Keychain path truncates values of
this length), records the paste time and member, and injects it as
`CLAUDE_CODE_OAUTH_TOKEN` into every Claude process on that machine. RCP warns
one month before the one-year mark and when a classified login failure names it.
Claude keeps its per-turn `stream-json` runtime; with a static token there is
nothing to rotate.

The credential startup gate stays for Claude's own writes and is otherwise
retired with the per-turn Codex processes.

### Login failure handling

`classify_agent_failure` matches `refresh_token_reused`, "refresh token was
already used", and the existing Codex signatures; Claude signatures are added
only from observed text and the first observed sample is recorded in the profile
comment. A task ending with `failure_kind=provider_auth` is never scheduled for
automatic recovery in either mode. Its episode gets `blocked_reason=sign_in`.
The machine account's owner state becomes `signed_out`. New launches for that
provider on that machine are refused with the reason. One notice appears on every
project's Runs view and in the project list. After `account/login/completed` or a
token paste, RCP clears the state and resumes every episode blocked on `sign_in`
through their exact recovery paths, without a human clicking Retry per turn.

### Wake policy

The orchestrator's inbox harvest returns both lifecycle notices and root-addressed
mail, and acknowledging them mid-turn consumes them so no wake follows. Lifecycle
notices of kind `experiment_episode stopped` whose stop the orchestrator itself
requested, and `experiment_replacement advanced` for a replacement the
orchestrator itself created, are recorded as acknowledged on creation. A child
task failure whose `failure_kind` is `provider_auth` creates a notice but never a
wake; the episode's `blocked_reason=sign_in` is the human-facing signal instead.
Every other trigger is unchanged, including the five-second grace window.

### Timeline

`GET /api/projects/{project_id}/episodes/{episode_id}/timeline` returns typed
events in time order: `turn` (role, cause, revision, status, attempt), `retry`
(parent turn), `wake` (cause: lifecycle, message, graph condition, watcher, with
the notice or message ids it delivered), `mail` (sender, recipient, body), `child`
(episode id, control node, event), `lifecycle` (ending fenced, ceiling reached,
recovery exhausted, wrap-up failed, reauthorized, signed out), and `human`
(stop, retry, reauthorize, message). Each event carries `at`, `actor`,
`parent_event_id` for branching, and the task, message, or child episode it
links to. The web holds one `EpisodeTimeline` model class that ingests those
events and a `TimelineRenderConfig` that maps event kinds to lane, glyph, color
token, and fold behavior, so a new visualization is a configuration change. Mail
bodies are folded by default and open on click; a task event opens the task
inspector; a child event opens the child episode. The component replaces the
Turns and Mail sections in the Runs card for both modes.

## Slices

All six slices land on one branch and one pull request, as a sequence of
commits in the order below, implemented with Codex (gpt-6-astra, medium),
reviewed by Claude and by the Codex GitHub reviewer after each push, verified as
listed, and merged by the human once. None is skipped.

### Slice 1: settle and honest state

Owners: `src/rcp/runs/auto_research.py`, `src/rcp/runs/episodes/reconcile.py`,
`src/rcp/runs/episodes/wrapup.py`, `src/rcp/api/episodes.py`,
`src/rcp/storage/episodes.py`, `web/src/types.ts` (add `blocked_reason`),
`web/src/campaigns.ts`, `web/src/components/CampaignRuns.tsx`,
`src/rcp/server_runtime.py` (logging handler).
Invariants: canonical Patch logs untouched; one transition per mutation; the
health literals do not change.
Checks: unit tests for compaction with oversized payloads; a reconcile test that
a failed wrap-up settles once and stays settled; serialization tests with the
production shape (`wrapping_up`, `exhausted`, `not_started`, last turn
succeeded) asserting `wrapping_up` then, after the reconciler runs,
`needs_action` with `wrapup_failed` or a report; a replay of the copied
production database on the team server through the new code showing the stuck
episode settle. `uv run pytest -n0` on the touched test files, `uv run ruff
check`, `npm --prefix web run build`.

### Slice 2: login failure handling

Owners: `src/rcp/providers.py`, `src/rcp/agents/failure_kinds.py`,
`src/rcp/runs/auto_research_recovery.py`, `src/rcp/runs/experiment_recovery.py`,
`src/rcp/api/episodes.py`, `src/rcp/api/experiment_controls.py`,
`src/rcp/api/index.py` and `src/rcp/projects.py` (machine-wide notice),
`web/src/views/*Runs*`, `web/src/components/AttentionRail.tsx`.
Invariants: agent capability unchanged; failure classification comes from the
provider's own text only.
Checks: signature tests with the recorded production error text; recovery tests
asserting no automatic attempt after `provider_auth`; projection tests for
`blocked_reason=sign_in`; a served-app check that the notice appears on every
project.

### Slice 3: the login owner

Owners: new `src/rcp/agents/codex_owner.py` (process, stdio multiplexer, thread
registry, supervision), `src/rcp/agents/codex_app_server.py` (becomes the
per-thread protocol adapter), `src/rcp/agents/launcher.py`, `src/rcp/background.py`,
`src/rcp/provider_skills.py`, `src/rcp/providers.py` (runtime selection collapses
for Codex), `src/rcp/agents/credential_gate.py`, `src/rcp/server_ops/provider_readiness.py`,
`src/rcp/server_ops/doctor*`, `src/rcp/config.py` (owner paths), new
`src/rcp/api/provider_login.py` (sign-in, sign-out, token paste, state),
`web/src/views/Settings*`, `web/src/components/AgentConfigControls.tsx`,
`src/rcp/agents/*prompt*.py` only where the runtime name is rendered.
Invariants: 4 (capability fixed in code; containment keys per process are the
same set as today, per thread the same write roots as today); 8 (one process per
data directory extends to one owner per machine account, held by an OS advisory
lock); 10g (a continuation resumes its exact thread or fails, never a fresh one).
Checks: the two spikes below rerun as tests against a fake app-server; a live
concurrent-turn check on the team server with a real sign-in through the UI; a
remote-machine check that the owner starts under `systemd_user`, survives the SSH
channel dropping, and is reattached; the skill and model probes shown to start no
process; `rcp server doctor` reporting owner and sign-in state.

### Slice 4: reauthorization continues the episode

Owners: `src/rcp/api/episode_routes.py`, `src/rcp/api/episodes.py`,
`src/rcp/storage/episodes.py`, `src/rcp/storage/auto_research.py`,
`src/rcp/storage/experiments.py`, `src/rcp/runs/auto_research_delivery.py`,
`src/rcp/runs/experiment_loop.py`, `src/rcp/agents/auto_research_prompt.py`,
`src/rcp/agents/experiment_loop_prompt.py`, `web/src/App.tsx`,
`web/src/components/CampaignRuns.tsx`, `web/src/components/ExperimentRunDetail.tsx`,
`web/src/api.ts`.
Invariants: 3 (only humans authorize); 6b (one transaction clears the ending and
raises the ceiling); the branch is never recreated.
Checks: storage tests for the atomic reauthorization; a test that a frozen child
completion is delivered after reauthorization; prompt data tests for the
`reauthorized` notice; a served-app check adding turns to the stuck production
episode's copy.

### Slice 5: wake policy

Owners: `src/rcp/runs/auto_research_effects.py`, `src/rcp/runs/auto_research_delivery.py`,
`src/rcp/storage/auto_research.py`, `src/rcp/agents/auto_research_prompt.py`.
Invariants: every paid wake still spends one unit; no wake is created by a
notice the orchestrator already acknowledged.
Checks: tests that mail is returned by harvest and consumed; that a self-caused
stop or replacement notice never admits a wake; that a `provider_auth` child
failure never admits a wake; prompt data tests.

### Slice 6: timeline

Owners: new `src/rcp/api/episode_timeline.py`, `src/rcp/storage/auto_research.py`
(read queries only), new `web/src/timeline.ts` (model class and render config),
new `web/src/components/EpisodeTimeline.tsx`, `web/src/components/CampaignRuns.tsx`,
`web/src/components/ExperimentRunDetail.tsx`, `web/src/styles.css`.
Invariants: read-only projection; no new authority.
Checks: API tests building the timeline of the production episode's copy and
asserting the six wakes carry their causes and the five retries nest under their
turns; `node --experimental-strip-types --test web/tests/timeline.test.mjs`;
served-app render of the production copy with a screenshot in the PR.

## Documents and prompts this contract changes

Reversed rules, each carried by a decision record: `docs/server.md` lines 253 to
321 (RCP does not log in to providers; never paste a token into RCP) and
`docs/specs/server-and-machine-operations.md` lines 57 to 63 (RCP does not
relocate or manage provider authentication).

Specs: `providers-and-containment.md` (owner per machine, `exec` and per-turn
app-server retired, fallback rule deleted, gate becomes supervision, signatures),
`auto-research-and-branch-merge.md` (continuation, wake policy, settle with a
visible wrap-up error, mail mid-turn), `conversations-episodes-and-watchers.md`
(Experiment-loop continuation), `api-web-and-desktop-projections.md`
(`blocked_reason`, timeline, sign-in state and flow, never `active` after an
ending), `interface-and-visual-design.md` (timeline), `projects-spaces-and-operations.md`
(who signs in, credential record), `compute-jobs.md` (the OS process owner also
supervises the login owner), `design.md` and the `AGENTS.md` registry (new
invariant: one credential-holding provider process per machine account; code
owner `src/rcp/agents/codex_owner.py`; test named in slice 3), `README.md` and
`docs/desktop.md` (sign-in through the UI, RCP-owned `CODEX_HOME`).

Prompts and skills: `src/rcp/agents/auto_research_prompt.py` (harvest includes
mail; the `reauthorized` notice; what no longer wakes it),
`src/rcp/agents/experiment_loop_prompt.py` (`human_reauthorization` now means the
same episode continues with more turns), `src/rcp/skills/episode-report/SKILL.md`
(a report can be superseded). Tests assert prompt data and enforcement, never
wording.

## Spikes already run

On codex 0.154.0, in a logged-out scratch `CODEX_HOME`, 2026-09-14:

- One `codex app-server` over stdio accepted two `thread/start` calls with
  different `cwd` and sandbox (`readOnly` and `workspaceWrite` with its own
  writable roots) and echoed each per thread. Two `turn/start` calls were
  accepted concurrently, both `inProgress`, and every notification carried its
  `threadId`. Both turns ran at once.
- `--listen unix://` answers a WebSocket upgrade (`101 Switching Protocols`) and
  closes a newline-JSON client; hence decision 12.
- `account/login/start` accepts `type=chatgptDeviceCode` and returns
  `userCode`, `verificationUrl`, and `loginId`; `account/login/completed`
  reports the result; `account/read` reports `requiresOpenaiAuth`.
- The per-process containment keys RCP names are the same for every project;
  per-thread parameters carry everything that differs.

Not yet run, and the first live checks of slice 3: two concurrent turns against
a real sign-in, and the owner under `systemd_user` on a remote machine attached
over SSH.

## Closure criteria

- The stuck production episode, replayed from a copy through slice 1, settles to
  `needs_action` with a report or a visible wrap-up error, and its card never
  says active.
- A recorded login failure classifies as `provider_auth` in both providers'
  tests, is never retried automatically, blocks new launches for that machine
  account, and is announced on every project.
- On the team server and on the desktop, one Codex owner process serves
  concurrent turns from two projects; the skill and model probes start no
  process; sign-in completes from the RCP UI with the device code and no shell;
  `rcp server doctor` reports the owner and sign-in state truthfully.
- Claude turns on the team server run under the pasted setup token and never
  write `.credentials.json`.
- "Add N turns" continues the copied production episode on its own branch with
  its own session, and the frozen child completion is delivered.
- Mail arriving mid-turn is read by that turn and causes no wake; a self-caused
  stop or replacement notice causes no wake; a child login failure causes no
  wake.
- The Runs card shows the timeline for the production copy with six wakes
  labelled by cause and five retries nested under their turns, and the Turns and
  Mail sections are gone.
- Every document and prompt listed above says what the code does.
