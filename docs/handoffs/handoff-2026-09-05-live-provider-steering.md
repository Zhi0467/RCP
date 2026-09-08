# Live provider steering handoff

Date: 2026-09-05
Status: active, human-confirmed on 2026-09-05. Phase 1 local probes are recorded
and committed as `44626b2`. Phase 2 implements exact-attempt human chat steering
for Codex app-server and Claude stream-json, durable human message receipts,
backend eligibility and disabled reasons, and composer delivery. Local provider
and API regressions pass. A served Codex Discuss turn verified delivered and
post-completion refused receipts; exec's disabled state was verified through
the API. Restart preserved those receipts and recovered an active exec turn as
paused with Resume/Retry available. S134 and current specs record the settled
steering contract; the retired open-questions document remains historical only.
The Chromium chat-component interaction drive also passes; its steer responses
are fixtures, not a live provider. Remaining verification includes the full
served-app browser/console drive, an integrated live Claude turn, Work
under a non-nested sandbox, restart with an unacknowledged steer, and all SSH
behavior. Probe (c) and SSH remain explicit gaps because this brief prohibits SSH. The
settled decisions below remain the contract; no persistent provider daemon or
hard interrupt is implemented. Phase 2 is committed on the PR branch; this
handoff remains active pending its incomplete acceptance drive.

## Evidence

Probed on 2026-09-05 against the installed binaries; re-probe before Phase 2
because app-server remains experimental.

- **Codex app-server, codex-cli 0.153.2.** The generated protocol schema defines
  `turn/steer` with `threadId`, `expectedTurnId`, `input[]`, and an optional
  `clientUserMessageId`. `expectedTurnId` is a precondition: the request fails
  when it is not the active turn. `turn/interrupt` with `threadId` and `turnId`
  also exists.
- **Claude Code 2.1.260.** `--input-format stream-json` is "realtime streaming
  input" and works only with `--print`.
- **RCP before Phase 2.** One fresh process per turn. The app-server runtime keeps the
  process's stdin open for the whole turn; the Claude stream-json runtime closes
  stdin after the prompt; Codex exec has no inbound channel. Read
  [durable task lifecycle](../specs/providers-and-containment.md#durable-task-lifecycle),
  [provider runtime selection](../specs/providers-and-containment.md#provider-runtime-selection),
  and [mail and lifecycle notices](../specs/auto-research-and-branch-merge.md#mail-and-lifecycle-notices).

## Settled decisions

1. **Target: the running human-triggered chat turn.** Discuss or Work, sent by
   the human who is watching it. Episode worker turns are out of v1: the spec
   says the human messages the orchestrator, not a child, and steering a worker
   would need the steer recorded in episode lineage and surfaced to the
   orchestrator as a notice. That is the named follow-up. Agent-to-agent
   steering stays ruled out by the current provider and orchestration specs.
2. **No persistent session.** Delivery is possible only while RCP's own provider
   process for that exact task attempt is alive. Every agent is still either
   running a turn or asleep with durable state; the recovery ladder, Pause,
   Resume, Retry, and restart safety are unchanged.
3. **Runtime matrix.** Codex app-server: `turn/steer` with the recorded thread
   id and the active turn id. Claude: stdin stays open and the turn is launched
   with `--input-format stream-json`; a steer is one user message on that stream.
   Codex exec, including the pre-prompt fallback from app-server: unsupported;
   the API reports the reason and the ordinary composer stays unavailable as
   for any running turn. Since 2026-09-07 there is no separate steering
   control and no rendered reason; the composer's Send addresses the running
   attempt while it can take input. The recorded actual runtime decides.
4. **Delivery is fail-closed.** RCP refuses when the addressed attempt is not
   running or the turn id does not match. It never queues a refused steer as the
   next turn and never resends after a disconnect. The receipt is one of
   **delivered** (provider acknowledged), **refused** (with reason), or
   **unknown** (the write began and no acknowledgment arrived, as after an SSH
   drop or process exit).
   Codex acknowledges through the `turn/steer` response and rejects a finished
   turn itself. Claude has no turn precondition, so RCP supplies it: launch with
   `--replay-user-messages`, treat the replayed user echo carrying the steer's
   UUID as the acknowledgment, write a steer only while no `result` event has
   been observed, and stop the process at the first `result` so a message that
   raced completion cannot start a new turn. When that `result` arrives before
   the acknowledgment deadline, a steer whose echo did not precede it is
   refused as completed before delivery. If the deadline expires first, RCP
   records unknown and cancels only the receipt waiter; a later echo or result
   does not rewrite that receipt or cause a resend.
5. **Record.** The steer is stored as the human's chat message, tagged with the
   task attempt and its receipt (invariant 11: it is a human message, not an
   answer). It carries no authority: it cannot upgrade Discuss to Work
   (invariant 10b) and changes no scope, graph target, budget, or permission.
6. **No hard interrupt in v1.** Stop keeps its graceful meaning; `turn/interrupt`
   is not wired.
7. **SSH.** The steer travels the same stdin pipe the app-server turn already
   uses through the SSH session. A dropped connection yields an unknown receipt.

## Phase 1: behavior probes

Use a throwaway project and `RCP_DATA_DIR` on a spare port. Never the human's
real data directory or running work. Record results in this section.

- **a.** App-server: send `turn/steer` during a long tool call. Record whether
  the input is applied at the next model step or interrupts the running command,
  the acknowledgment timing, and the response when sent after `turn/completed`.
- **b.** Claude: run `--print --input-format stream-json --output-format
  stream-json`, send a second user message mid-turn. Record when it is applied,
  whether stdin must stay open for the turn to continue, and the behavior after
  the result message. If Claude cannot apply mid-turn input, Claude becomes
  unsupported in v1 and the contract above does not change.
- **c.** Both over SSH through the existing wrapper, dropping the connection
  mid-steer, to confirm the unknown receipt and that nothing is resent.

### Local probe receipts (2026-09-05 America/New_York)

Executed only in `$SCRATCH/steering-probes/project`, where:

```sh
SCRATCH=<disposable session scratch directory, not retained>
PROBES=$SCRATCH/steering-probes
codex --version  # codex-cli 0.153.2
claude --version # 2.1.260 (Claude Code)
codex app-server generate-json-schema --out "$PROBES/schema"
uv run python "$PROBES/probe.py" codex
uv run python "$PROBES/probe.py" claude
uv run python "$PROBES/probe_dynamic.py" codex
uv run python "$PROBES/probe_eof.py" claude
```

The probe drivers and JSONL wire captures lived in the session scratch
directory and are not retained; the receipts below quote the messages that
matter. They launched and terminated only their own fresh processes; no provider login,
credentials, SSH host, RCP server, or real RCP data was touched. Timings below are
monotonic seconds since each launch. These are direct protocol probes; served-app
verification belongs to Phase 2. Setup (`npm ci`, managed Chromium install, web
build, `uv sync`) also exited 0.

**(a) Codex.** Exact process command (existing RCP Discuss adapter):

```sh
/opt/homebrew/bin/codex app-server --stdio --disable apps --disable multi_agent \
  --disable plugins --config 'web_search="live"' \
  --config 'approval_policy="never"' --config 'sandbox_mode="workspace-write"' \
  --config sandbox_workspace_write.network_access=true
```

The driver sends `initialize` (experimental API enabled), `initialized`,
`config/read`, `thread/start` with the existing adapter's containment overrides,
and `turn/start` in the throwaway cwd. First attempts are retained:
`codex-initial.jsonl` used `gpt-5.4-mini`, which answered without the requested
sleep; `codex-unsupported-model.jsonl` used `gpt-5.4`, rejected by this ChatGPT
account; `codex-sandbox.jsonl` used the configured `gpt-6-astra`, whose shell
reported `sandbox-exec: sandbox_apply: Operation not permitted` (exit 71).
None of these establishes behavior during a running shell command.

The final probe (`codex-dynamic.jsonl`) used the configured model and one
throwaway `dynamicTools` function, `probe_wait`, implemented as a 12-second
asynchronous wait in the driver. It has no shell or filesystem access. This
measures a pending provider tool call without disabling containment. The prompt
requests that tool once and asks for any subsequent human instruction afterward.

- Thread `01a07424-b0cc-78e0-97d0-3aa4a8365e1b`, turn
  `01a07424-b127-7bd2-a40e-d80fffa5c52f`.
- At 5.110s: `item/tool/call` request id 0, tool `probe_wait`.
- At 5.111s: send the following; acknowledgment arrives in the same recorded
  millisecond (under 1ms at this measurement resolution):

```json
{"id":90,"method":"turn/steer","params":{"threadId":"01a07424-b0cc-78e0-97d0-3aa4a8365e1b","expectedTurnId":"01a07424-b127-7bd2-a40e-d80fffa5c52f","input":[{"type":"text","text":"After the current wait finishes reply only STEERED instead of ORIGINAL. Do not call another tool."}]}}
{"id":90,"result":{"turnId":"01a07424-b127-7bd2-a40e-d80fffa5c52f"}}
```

- The tool finishes normally at 17.112s; steering did not cancel the pending
  tool. The next model step answers `STEERED`; `turn/completed` at 20.039s.
- A second `turn/steer`, id 91, with the same thread and expected turn is sent
  at 20.039s. At 20.040s it returns
  `{"error":{"code":-32600,"message":"no active turn to steer"},"id":91}`.
- stdin closes; the process exits 0 at 20.051s, empty stderr.
- **Gap:** a real Codex shell command could not run under the nested macOS
  sandbox. The pending dynamic-tool drive passed; shell-interruption behavior
  is not claimed as verified.

**(b) Claude.** Exact process command:

```sh
/Users/zhiwang/.local/bin/claude --print --input-format stream-json \
  --output-format stream-json --verbose --replay-user-messages --safe-mode \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --no-session-persistence --permission-mode dontAsk \
  --allowedTools 'Bash(sleep 12)' --model haiku --effort low
```

Input is newline-delimited
`{"type":"user","message":{"role":"user","content":"..."},"uuid":"<unique UUID>","parent_tool_use_id":null,"session_id":""}`.
The first prompt requests `sleep 12` once and then any subsequent human message.
`claude.jsonl` records:

- At 4.893s, after Bash tool use begins, send user UUID
  `ae6aef36-54b2-42b2-9931-4ddb8b096401` with
  `After the current sleep finishes reply only STEERED instead of ORIGINAL. Do not run another command.`
- At 17.184s the tool result reports `interrupted:false`, no output, no error.
  At 17.188s the steer is echoed as `type:user`, `isReplay:true`, same UUID:
  acknowledgment delay 12.295s. At 20.699s `type:result`, `subtype:success`,
  `result:STEERED`, `queued_turn_count:0`; both input UUIDs are attributed.
- stdin stays open through the result. Send post-result UUID
  `162cb4c9-fa53-4121-bc1c-32d614eb4ae3` at 20.699s. Echo at 22.459s;
  a **new** result `LATE` arrives at 23.367s. EOF then exits 0 at 23.657s.
- A separate EOF check (`claude-eof.jsonl`) closes stdin immediately after the
  mid-tool steer at 3.385s. The existing turn still finishes `STEERED` at
  18.516s and exits 0 at 18.739s. Keeping stdin open is necessary for future
  input, not for already accepted work to finish.
- The initial stronger `reply only ORIGINAL` prompt produced `ORIGINAL` despite
  acknowledging both UUIDs (`claude-initial.jsonl`). The revised prompt above
  distinguishes message application from conflicting output instructions.
- **Contract consequence:** mid-turn application works, but the stream has no
  `expectedTurnId` precondition, and a write racing completion starts a new turn.
  Decision 4 therefore makes the replayed echo the acknowledgment and stops the
  process at the first `result`.

**(c) SSH: not run.** The implementing brief prohibits every SSH host. Connection
loss over the existing SSH wrapper, remote receipt behavior, and no-resend
verification over SSH remain unverified; simulated transport tests cannot close
that gap.

## Phase 2: owners and file scope

- `src/rcp/providers.py`: a per-runtime steer capability and a `ProviderTurn`
  method that renders a steer or reports unsupported.
- `src/rcp/agents/codex_app_server.py`: the `turn/steer` request, active turn
  id tracking, and response-to-receipt mapping.
- `src/rcp/agents/launcher.py`: an inbound writer for a running attempt, stdin
  kept open when the runtime supports steering, and receipt events.
- Task engine and API: one route that steers an exact task attempt, persistence
  of the message and receipt, and the disabled state in the projection.
- Web: steer input while the turn runs and receipt display. The disabled reason
  comes from the backend; `web/src/types.ts` seals the receipt state.
- Specs in the same PR: durable task lifecycle and runtime selection gain the
  steer channel; conversation human input gains the record; the mail section's
  "nothing is injected into a live provider process" is narrowed to notices and
  mail; retire the former live-input deferral wherever current docs still state it.


## Phase 2: local verification receipts

All served-app requests used `http://127.0.0.1:8432` and the disposable project
`$SCRATCH/steering-live-project`. The app was launched with:

```sh
RCP_DATA_DIR="$SCRATCH/steering-data" uv run rcp serve \
  "$SCRATCH/steering-live-project" --host 127.0.0.1 --port 8432
uv run python "$SCRATCH/drive-live-steering.py"
```

The project chat profile selected `provider=codex`, `runtime=app-server`,
`model=gpt-6-astra`, `reasoning=low`, `run_on=local`. A named human identity was
set only in the disposable data. The Discuss prompt requested 40 numbered
observations about apples and oranges, no tools or file access, and compliance
with any subsequent human instruction. Times are seconds after the API drive
started on 2026-09-05 America/New_York:

- At 0.077s, task `6f0134f0-4db9-488f-8179-e8dc16d0b3b4` was accepted (HTTP
  202), chat `39888553-a9cf-448f-8b01-4c25b8647ce1`, attempt 1.
- At 0.629s, its GET projection reported `can_steer:true`, actual runtime
  `codex.app-server-stdio.v1`, turn `01a0743f-7b40-7c13-b2e1-26e7c4357e78`.
- POST `/api/projects/346aa4c3-e183-4c75-b731-d6407ad2ef7f/tasks/6f0134f0-4db9-488f-8179-e8dc16d0b3b4/steer`
  addressed that exact attempt and turn with message UUID
  `ee8f723d-de8c-47d7-a701-3c3803c73ae3`, text
  `Stop the list. Reply exactly LIVE_STEER_ACK. Do not use tools.`
  At 0.666s, HTTP 200 returned the stored human message and
  `steering:{attempt:1,turn_id:"01a0743f-7b40-7c13-b2e1-26e7c4357e78",status:"delivered",label:"Delivered",reason:null}`.
- The task succeeded at 45.847s. Its answer contained the complete list followed
  by `LIVE_STEER_ACK`: acknowledgment establishes delivery, not immediate
  interruption or literal compliance with the instruction to stop the list.
- At 45.871s, a second POST to the same completed attempt returned HTTP 200,
  message UUID `7817deed-2e60-41af-9ea1-29a25055304e`, receipt `refused`, reason
  `This task attempt is not running.` No second provider turn was started.
- At 45.886s, GET of the canonical conversation contained the original prompt,
  one delivered human steer, the assistant answer, and one refused human steer.
  Both receipts retained attempt/turn identity and every message retained
  `mode:discuss`. The driver assertions exited 0.
- Updating the disposable project-chat profile to `runtime=exec` and starting
  Discuss task `a57368b2-0c91-40f3-b9aa-250edce78ee5` produced an active GET
  projection with `runtime_id:codex.exec-json.v1`, `steer_visible:true`,
  `can_steer:false`, reason `Codex exec does not support live steering.` This
  verifies the backend disabled state; browser rendering remains unverified.

Provider subprocess regressions exercise both protocols through real local
fixture pipes: exact-turn delivery, completion refusal, process-loss unknown,
no resend, and Claude termination before a second result. A local fixture also
exercises the remote dispatch branch and makes failure to confirm remote stop
an observable task error. The shipped remote termination helper verifies whole
process-group exit, escalates TERM to KILL within bounded waits, and is tested
against owned local groups including one that ignores TERM; no SSH host is
contacted. API regressions cover durable
receipt folding, identity/membership, unchanged authority, duplicates during a
pending acknowledgment, lost receipt persistence, and a restarted engine with
no live writer. These simulations do not establish remote behavior.

A direct live Claude drive through `AgentLauncher.stream`, model `haiku`,
`reasoning=low`, Discuss, and the throwaway probe directory stopped at readiness
with `claude CLI is not authenticated. The same readiness refusal reproduced outside the sandbox on the
committed branch, so the integrated Claude steer is blocked by this machine's
RCP Claude readiness state, not by the sandbox; the Phase 1 direct protocol
probe remains the real-Claude evidence.` (1.661s, driver exit 1). No Claude turn
was started and credentials were not changed. Phase 1's real replay-echo probe
remains valid evidence; live verification of the integrated launcher is open.

The served-app API requests above returned 200/202 without unexplained failures;
server logs were inspected; browser icon probes returned unrelated 404s. Safari
rendered the disposable project home, but
further automation returned `noWindowsAvailable` and then concurrent-user-change
protection. The full web suite's Chromium drives could not launch under the
sandbox (`bootstrap_check_in ... Permission denied (1100)`). The steer UI,
disabled composer pixels, browser network inspector, and console drive are gaps.
Work/tool verification remains a gap because nested Codex shell execution is
blocked; Discuss was used as directed. App restart during a running turn is
verified for the active exec Discuss turn: Ctrl-C through the owned launch
session stopped the server, and a fresh server on 8432 with the same disposable
data reported the same attempt as `paused`, `can_resume:true`, `can_retry:true`,
`can_steer:false`, native session `01a07445-c89a-7c33-b229-8eaf93059d2a`, and its
retained `run-stage/chat-baae28352b6d735a-fd6fc009-c8c1-4a58-be44-a6d551833627`
scratch. The original Codex chat still had the identical delivered and refused
message UUIDs and receipts. No Resume or Retry was dispatched. The first
shutdown logged `KeyboardInterrupt` / `asyncio.exceptions.CancelledError`;
restart completed successfully. Restart with an unacknowledged live steer is
covered only by regressions, not this served drive. Probe (c), real SSH
receipt/loss behavior, and SSH no-resend verification were not run, as instructed.
The restarted disposable server shut down cleanly; port 8432 had no listener
after cleanup.

## Acceptance

Review verification on 2026-09-06 UTC (unchanged implementation `bc2e536`):

- `uv run pytest tests/test_api_steering.py tests/test_provider_steering.py
  tests/test_remote_terminate_provider.py tests/test_launcher.py -q` passed all
  96 tests. An initial parallel run failed one SIGTERM-ignoring process-group
  absence check; all 15 helper tests passed serially and the complete unchanged
  focused rerun passed. No production timeout was changed to hide this transient.
- `node --test web/tests/liveSteering.browser.test.mjs` passed outside the
  sandbox. It serves the real `NodeChat` component through Vite and drives
  Chromium: exact attempt/UUID, preserved next-turn draft, delivered and refused
  receipts, disconnected response without resend, and disabled exec all pass
  with no page errors. The endpoint responses are fixtures; this closes the
  component interaction gap, not the served-app/live-provider browser drive.
- PR CI for that head passed lint/format, Python 3.11 and 3.12, old-data upgrade,
  and web typecheck/tests. No SSH host, provider credentials, or production data
  was used in this review.

[S134](../acceptance/S134-steer-the-running-human-chat.md) is the single new,
human-confirmed acceptance scenario. It covers app-server locally and over SSH,
completion refusal, unknown delivery with no resend, exec disabled state,
durable human receipts, Claude completion fencing, and unchanged restart
recovery. It remains `blocked-external`; the unverified portions above prevent
a complete pass.

## Closure condition, all of it

1. Phase 1 results are recorded above.
2. The acceptance scenario is confirmed and passing, and the specs are
   updated.
3. One real steer was exercised locally and over SSH, with receipts in this file.
4. This handoff is archived in the same PR that completes item 3.
