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
invariants: [4, 4b, 5, 7, 9, 10b, 10c, 10d, 10g, 11]
reported_by: human-confirmed queued-follow-up brief on 2026-09-08
---

# Send during a running human chat with an honest delivery receipt

The human can correct the ordinary Discuss or Work turn they are watching
by injecting into Codex app-server or queueing a Claude follow-up turn. Delivery
is bounded to RCP's existing provider process for the exact task attempt, and the stored human message tells whether
the provider acknowledged it. No receipt implies changed authority or that the
model obeyed the input.

This journey was confirmed in the
[implementation handoff](../handoffs/handoff-2026-09-08-claude-queued-follow-up.md).
The complete drive remains blocked: real SSH steering and connection-loss
verification need an authorized reachable host. Local protocol probes alone do
not establish the served-app or SSH promises below.

## Drive — live local and SSH providers

1. In disposable project data, select Codex app-server for the ordinary chat
   profile. Start a Discuss turn long enough to receive another instruction.
   While it runs, type a recognizable requested answer change into the ordinary
   composer and send it; the send button is labelled **Steer running turn**.
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
4. Select exec, start a turn, and confirm the composer stays unavailable as for
   any running turn while the composer displays the backend's unsupported-runtime
   reason. Pressing Enter leaves the draft intact and sends no request. Repeat
   with a pre-prompt app-server failure that falls back to exec: the actual exec
   runtime governs the composer.
5. Select Claude stream-json and start a long Discuss turn. The composer becomes
   ready on the initial command's `started` lifecycle, before its replay echo,
   and visibly says **Queue a follow-up turn**, matching the send-button label.
   Send a question about a recognizable detail in the original prompt. Its
   matching `queued` lifecycle immediately establishes **Queued**, with a reason
   saying it runs after the current turn. The first result answers the original
   prompt; the follow-up then runs in the same session and remembers the detail.
   Both answers appear in one assistant message joined by a blank line, with
   separate usage rows. Repeat with Work and verify the same scope and stage,
   one final Patch read, and no extra graph-change channel. Mode controls cannot
   change the running capability. With no follow-up, the first result still
   ends the process. Race unacknowledged input with the final result: it is
   **Refused** when that result closes the invocation before the deadline.
   Missing usable result UUIDs also stop the process; foreign lifecycle UUIDs
   never keep it open. A timed-out receipt remains **Unknown** without resend.
6. Repeat the app-server steer on an authorized SSH execution host using RCP's
   existing wrapper. Observe the matching acknowledgment, changed answer, and
   persisted delivered receipt. No independent provider daemon is started.
7. On an owned throwaway SSH turn, drop the transport after a steer write begins
   and before its acknowledgment. The stored receipt becomes **Unknown**.
   Restore connectivity and reload the app: RCP does not resend the steer or
   start a replacement turn. Repeat for Claude without a matching `queued` lifecycle or
   observed result. A local process exit before acknowledgment follows the same
   unknown rule; Claude's result observed before the acknowledgment deadline
   uses the explicit refusal in step 5.
8. Restart the disposable RCP app during an owned running turn. The existing
   interrupted-attempt recovery and retained scratch remain available through
   Pause, Resume, and Retry as applicable. A recovery attempt retains the
   original mode, graph target, session, and scope; it does not revive the old
   steer writer or resend an unknown steer. Graceful Stop keeps its existing
   fence rather than issuing a hard provider interrupt.
9. Open an episode worker. Its composer cannot steer it, and a direct
   request cannot deliver input to it. The human still addresses the
   orchestrator through ordinary mail; mail and lifecycle notices do not enter
   a running provider through this channel.
10. Inspect browser console, application requests, and server logs throughout.
    Expected delivery refusals remain visible receipts; unrelated request or
    runtime failures are recorded separately and never counted as delivery.

## Assert — transport, API, and browser

- `only_the_exact_live_human_chat_attempt_can_receive_a_steer`
- `actual_runtime_supplies_visible_action_label_and_disabled_reason`
- `codex_acknowledgment_matches_the_expected_active_turn`
- `claude_started_unlocks_and_queued_acknowledges_a_same_session_follow_up`
- `claude_results_join_one_answer_and_only_owned_outstanding_commands_continue`
- `stored_human_message_retains_one_exact_attempt_receipt_after_reload`
- `completion_refuses_without_queuing_a_new_turn`
- `disconnect_is_unknown_and_never_automatically_resent`
- `restart_and_graceful_stop_keep_the_existing_recovery_contract`
- `steering_never_changes_capability_or_reaches_episode_workers`
- `no_unexplained_console_or_application_request_errors`
