# Voice keeps member-private text transcripts

Date: 2026-10-08. Status: active. Its live checks are among
[the open live checks](../handoffs/README.md).

## Decision

RCP saves the text of each voice session so the member can list and resume
it. It keeps no audio. The text is a **member service record**, owned by the
same store as the member's service connections and voice settings:

- It is stored on the current space's backend. For a team space that is the
  team server, under the member's private folder.
- Only that member's routes read or change it. Other members cannot.
- It keeps the last 20 sessions, each for up to 30 days. It survives sign-out
  and is deleted when the member is removed. The member can delete any
  session. Disconnecting the voice provider keeps it.
- Resume sends selected text back to OpenAI as `session.input` of a new
  session. RCP keeps sending `store: false`.

Resume opens a new OpenAI session. OpenAI offers no way to reattach a dropped
GPT-Live session. Its fork needs `store: true`, which keeps the recording at
OpenAI for 30 days and fails on keys with zero data retention or storage off.

## What this reinterprets

The voice spec said RCP never receives the audio and keeps no transcript, and
that the session exchange is stateless. Audio still never reaches RCP. The
backend now holds the session's text record and a short list of action
receipts: which tool ran, on what, with which task or episode id, and its
outcome.

## Why

- **A dropped session was gone.** A network blip or a closed window lost the
  whole conversation, and voice sessions appeared nowhere.
- **Receipts prevent repeats.** Without a record of a Work send whose response
  was lost, a resumed session could send it again.
- **It works with any key.** Seeding needs no OpenAI-side storage.

## What it gives up

- **The service account can read it.** "Private" means private to the member
  through RCP, not hidden from whoever operates the server.
- **Resume resends text to OpenAI.** OpenAI's own abuse-monitoring retention
  applies to it, as to any session; `store: false` is not zero data retention.
- **Resume loses tone and timing.** The new session gets text only, cut to
  OpenAI's limits (128 messages, 8,192 tokens), newest kept.
- **Seeded history is context, not authority.** It never authorizes a write or
  proves current status. Quoted project content keeps its source.
