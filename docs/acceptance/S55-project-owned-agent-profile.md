---
id: S55-project-owned-agent-profile
status: implemented
tier: hermetic
driver: pytest + browser
covered_by: tests/test_canonical_machine.py, web/tests/paperAndChatProfile.test.mjs,
  web/tests/chatWorkspace.test.mjs
invariants: [4, 10]
reported_by: human, 2026-08-03
last_passed: 2026-08-03
---

# Project Settings owns agent configuration

Confirmed by the human on 2026-08-03.

Provider, model, reasoning, and execution machine are configured in Project
Settings. Seed and Refresh keep their deliberate launch controls. Chat and the
paper coach show only the provider name and offer no per-conversation agent
configuration; the chat's Raw truth inputs control remains because it selects
turn context, not execution configuration.

Changing a provider without explicitly selecting a model clears the previous
provider's model before launch. A caller may still supply an explicit model for
the new provider. Settings supplies the default for a fresh conversation. An
existing native conversation retains the profile it last ran with, including
when it continues or resumes, so changing Settings never silently retargets an
already-established provider session.

## Drive

1. Configure a chat profile as Codex with an explicit Codex model in Settings.
2. Change the profile provider to Claude without selecting a Claude model and
   save.
3. Open a new project and node chat, inspect the resting provider label, change
   Raw truth inputs, and send a turn.
4. Open Paper and inspect the coach controls. Resume an existing coach session.
5. Open Seed/Refresh and Settings again.

## Assert — pytest

- A provider override with `model=None` clears an inherited model when the
  provider differs from the stored profile.
- An explicit model supplied with the provider override is retained.
- Callers pinned to resolved profiles and Settings saves keep working.

## Assert — browser

- Chat and coaching show a non-expandable provider-name label only.
- Chat offers no provider, model, reasoning, or machine control.
- Raw truth inputs remains usable.
- Settings still configures all four fields, and Seed/Refresh retains its launch
  configuration.
- A fresh chat uses the current Settings profile; an existing conversation
  continues with its last persisted profile and shows that provider truthfully.
- The launched task uses Claude's provider default rather than the old Codex
  model.
- No console, network, or server error occurs.

## Failure means

Conversation-local state overrides project policy, switching providers launches
an incompatible stale model, or removing the picker also removes the turn's
truth-scope control.
