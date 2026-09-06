---
id: S134-steer-the-running-human-chat
status: blocked-external
tier: remote
driver: pytest + api + browser + ssh
covered_by:
  - tests/test_provider_steering.py
  - tests/test_api_steering.py
  - web/tests/liveSteering.browser.test.mjs
  - web/tests/api.test.mjs
  - web/tests/agentTasks.test.mjs
invariants: [4, 4b, 5, 7, 9, 10b, 10d, 10g, 11]
reported_by: human-confirmed live-provider-steering handoff on 2026-09-05
---

# Steer the running human chat with a durable delivery receipt

The human can correct the ordinary Discuss or Work turn they are watching
without starting a second turn. Delivery is bounded to RCP's existing provider
process for the exact task attempt, and the stored human message tells whether
the provider acknowledged it. No receipt implies changed authority or that the
model obeyed the input.

This journey was confirmed in the
[implementation handoff](../handoffs/handoff-2026-09-05-live-provider-steering.md).
The complete drive remains blocked: real SSH steering and connection-loss
verification need an authorized reachable host. Local protocol probes alone do
not establish the served-app or SSH promises below.

## Drive — live local and SSH providers

1. In disposable project data, select Codex app-server for the ordinary chat
   profile. Start a Discuss turn long enough to receive another instruction.
   While it runs, use **Steer running turn** and **Send steer** with a
   recognizable requested answer change.
   Confirm that the active turn applies it and that the human message shows
   **Delivered** only after the matching provider acknowledgment.
2. Reload the conversation and read its stored chat record through the API.
   The steer remains one human message with the same text, exact addressed
   attempt and turn, and delivered receipt. Its text is neither an assistant
   answer nor a provider trace. Repeat on a Work turn and confirm the original
   mode, graph target, write roots, and permission remain unchanged. A Discuss
   steer asking for Work authority does not upgrade the turn.
3. After completion, address the same attempt with another steer. Confirm a
   stored **Refused** receipt names completion, no provider turn starts, and the
   message is not queued for the next ordinary turn. Address an old attempt
   while a newer one runs; refusal must not redirect the message to the new one.
4. Select exec, start a turn, and confirm the control is disabled with the
   backend's unsupported-runtime reason. Repeat with a pre-prompt app-server
   failure that falls back to exec: the actual exec runtime governs the control.
5. Run the equivalent mid-turn drive with Claude stream-json. Confirm the
   replayed user echo bearing the steer UUID establishes **Delivered**. Race
   input with completion: the first result ends the process, an input without
   its earlier matching echo is **Refused** as completed before delivery, and
   no second Claude result or new turn is produced.
6. Repeat the app-server steer on an authorized SSH execution host using RCP's
   existing wrapper. Observe the matching acknowledgment, changed answer, and
   persisted delivered receipt. No independent provider daemon is started.
7. On an owned throwaway SSH turn, drop the transport after a steer write begins
   and before its acknowledgment. The stored receipt becomes **Unknown**.
   Restore connectivity and reload the app: RCP does not resend the steer or
   start a replacement turn. Repeat for Claude without a replayed echo or
   observed result. A local process exit before acknowledgment follows the same
   unknown rule; Claude's observed result uses the explicit refusal in step 5.
8. Restart the disposable RCP app during an owned running turn. The existing
   interrupted-attempt recovery and retained scratch remain available through
   Pause, Resume, and Retry as applicable. A recovery attempt retains the
   original mode, graph target, session, and scope; it does not revive the old
   steer writer or resend an unknown steer. Graceful Stop keeps its existing
   fence rather than issuing a hard provider interrupt.
9. Open an episode worker. No live steering control is available, and a direct
   request cannot deliver input to it. The human still addresses the
   orchestrator through ordinary mail; mail and lifecycle notices do not enter
   a running provider through this channel.
10. Inspect browser console, application requests, and server logs throughout.
    Expected delivery refusals remain visible receipts; unrelated request or
    runtime failures are recorded separately and never counted as delivery.

## Assert — transport, API, and browser

- `only_the_exact_live_human_chat_attempt_can_receive_a_steer`
- `actual_runtime_supplies_support_and_disabled_reason`
- `codex_acknowledgment_matches_the_expected_active_turn`
- `claude_uuid_echo_acknowledges_before_the_first_result_fence`
- `stored_human_message_retains_one_exact_attempt_receipt_after_reload`
- `completion_refuses_without_queuing_a_new_turn`
- `disconnect_is_unknown_and_never_automatically_resent`
- `restart_and_graceful_stop_keep_the_existing_recovery_contract`
- `steering_never_changes_capability_or_reaches_episode_workers`
- `no_unexplained_console_or_application_request_errors`
