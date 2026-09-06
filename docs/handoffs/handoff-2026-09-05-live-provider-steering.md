# Live provider steering handoff

Date: 2026-09-05
Status: active, human-confirmed on 2026-09-05. Phase 1 local probes (a) and (b)
are recorded below. Probe (c) and every SSH verification remain open because
this work session had no SSH host. The Claude completion race found in (b) is
settled in decision 4. No implementation code exists yet; Phase 2 starts from
the owners below. This supersedes the deferral formerly recorded as open
question Q8 for the modest version only: the human may send a message into the
chat turn they are watching, through the provider process RCP already owns for
that turn. No persistent session daemon is authorized. Phase 1 probes precede
Phase 2 implementation, in one PR of their own.

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
- **RCP today.** One fresh process per turn. The app-server runtime keeps the
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
   steering stays ruled out (Q8, Q9).
2. **No persistent session.** Delivery is possible only while RCP's own provider
   process for that exact task attempt is alive. Every agent is still either
   running a turn or asleep with durable state; the recovery ladder, Pause,
   Resume, Retry, and restart safety are unchanged.
3. **Runtime matrix.** Codex app-server: `turn/steer` with the recorded thread
   id and the active turn id. Claude: stdin stays open and the turn is launched
   with `--input-format stream-json`; a steer is one user message on that stream.
   Codex exec, including the pre-prompt fallback from app-server: unsupported;
   the control is disabled and says why. The recorded actual runtime decides.
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
   raced completion cannot start a new turn. A steer whose echo did not arrive
   before that `result` is refused as completed before delivery.
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
SCRATCH=/private/tmp/claude-501/-Users-zhiwang-research-RCP/24a19e4a-5908-4790-90c1-1740a6637057/scratchpad/codex-runs
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
  mail; retire the Q8 deferral wherever current docs still state it.

## Acceptance

This is a new durable cross-module promise, so it needs one acceptance scenario
at the next free number. It must cover: a steer applied on app-server locally and
over SSH; refusal after completion; an unknown receipt after a disconnect with
nothing resent; the disabled control on exec; the stored message and receipt;
and an app restart during a turn leaving the recovery ladder unchanged.

## Closure condition, all of it

1. Phase 1 results are recorded above.
2. The acceptance scenario is confirmed and passing, and the specs are
   updated.
3. One real steer was exercised locally and over SSH, with receipts in this file.
4. This handoff is archived in the same PR that completes item 3.
