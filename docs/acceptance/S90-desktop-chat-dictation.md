---
id: S90-desktop-chat-dictation
status: pending
tier: live
driver: desktop
covered_by:
  - web/src-tauri/src/dictation.rs
  - web/tests/chatInput.test.mjs
  - web/src/components/NodeChat.tsx
  - desktop 2026-08-08 — rebuilt shell and control placement; live audio outstanding
invariants: [10d]
reported_by: human, 2026-08-08
---

# A spoken thought becomes an editable chat draft

Desktop chat dictation is input assistance, not a voice agent mode. The macOS
Speech service converts one bounded microphone segment into ordinary editable
composer text. RCP never sends the turn automatically and never stores audio.

## UI path

Confirmed by the human on 2026-08-08.

1. Open any node or project chat in the macOS desktop app. A microphone button
   appears immediately left of Send. The browser entrance has no RCP microphone
   button because an ordinary page cannot call the native Apple Speech API.
2. Place the cursor anywhere in an existing draft and click the microphone.
3. On first use, macOS requests microphone and speech-recognition permission.
   RCP uses Apple's network-backed recognition service without an API key or
   speech-model download and retains no audio.
4. Speak mixed English and Chinese. Recognition is best-effort using the Mac's
   default speech locale. RCP offers no language picker.
5. Partial results appear at the captured cursor position. Revisions to a
   partial result replace only that dictated span and never overwrite existing
   typed text elsewhere.
6. Click Stop. All partial and final text remains in the draft and can be
   edited. There is no cancel or discard gesture.
7. Start another segment at a different cursor position. At 55 seconds a live
   segment stops automatically and preserves everything received.
8. Navigation, window hiding, provider unavailability, network failure, Apple
   throttling, or permission denial stops recognition visibly without removing
   typed or dictated draft text.
9. Send normally through Discuss or Work. From that point onward dictated text
   is indistinguishable from typed human text.

## Assertions

- `desktop_only_microphone_button_is_left_of_send`
- `start_and_stop_are_the_only_recording_actions`
- `partial_results_update_only_the_captured_cursor_span`
- `every_partial_and_final_result_is_preserved_as_draft_text`
- `dictation_never_sends_the_turn`
- `dictation_stops_at_fifty_five_seconds_without_losing_text`
- `permission_and_service_failures_preserve_the_draft`
- `rcp_retains_no_audio_and_requires_no_api_key_or_model_download`
- `mixed_english_chinese_input_is_best_effort_under_the_default_locale`

## Failure means

RCP loses draft text, records past Stop, overwrites unrelated text, stores
audio, requires an API key or model download, exposes a broken browser control,
or sends the message without the human pressing Send.
