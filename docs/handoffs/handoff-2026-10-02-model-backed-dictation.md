# Model-backed dictation

Date: 2026-10-02
Status: design settled with the human on 2026-10-02 (issue #229, part 1), then
revised the same day after an astra xhigh review and a Claude review.
Nothing is implemented yet. The standby voice agent (part 2) is a separate
handoff and PR; it reuses this PR's service connections, member settings file,
and microphone owner.

Close this handoff when all of these hold on real hardware:

- the desktop app in a personal space dictates through a connected OpenAI or
  Groq key, and the text lands in the editable span;
- a team member dictates the same way from the browser app and from the phone
  web app, each with their own key;
- a Gemini connection dictates from at least one client;
- on macOS 26 or later, macOS dictation runs on-device through SpeechAnalyzer,
  including the first-use model download;
- `RCP Candidate.app` connects a service (proving the bundled check clips ship),
  and launches and dictates on a Mac running macOS 13, 14, or 15 through the old
  recognizer.

## Settled

- **Services.** Two adapters:
  - **OpenAI-compatible** `/v1/audio/transcriptions`, with presets for OpenAI
    (`gpt-transcribe`), Groq (`whisper-large-v3-turbo`), and a custom server
    (base URL, optional key, model).
  - **Gemini**, through `generateContent` with the Gemini 3.5 Transcribe model
    and a Gemini API key. Gemini's OpenAI compatibility layer has no
    transcription endpoint, so it cannot share the first adapter.
  - No other service in this PR.
- **Keys belong to members.** Each member connects their own services and pays
  for their own use. A personal space has one owner. See the
  [decision record](../decisions/2026-10-02-transcription-keys-belong-to-members.md).
- **Custom server addresses.** `https`, or plain `http` only to loopback. On a
  team space the server makes the call, so loopback means the team server. No
  LAN `http`.
- **Text after you stop.** Network services are batch only: record, stop, a
  short "Transcribing…" state, then the text. macOS dictation keeps its live
  partial text.
- **No vocabulary hints** and no price display in this PR.
- **macOS 26 moves to SpeechAnalyzer in this PR.** macOS 13 to 15 keep today's
  recognizer. End users install nothing.
- **Older SDK builds without it.** A source build without the macOS 26 SDK
  skips SpeechAnalyzer and warns; release builds require it.
- **Existing rules carry over.** Dictation never sends a message. RCP never
  stores audio. Audio attachments stay refused. RCP cannot promise anything
  about a service's own retention, and the connect dialog says so.

## Clients

| Client | macOS dictation | Network services |
|---|---|---|
| Desktop app, personal or team | yes | yes |
| Team browser app (over the SSH tunnel) | no | yes |
| Team phone web app | no | yes |
| Personal paired phone | no | no; it stays notify-only |

Each member picks one dictation service: **macOS** or one of their
connections. A new member, or one whose settings were lost in a restore, is on
macOS. The desktop app honors either choice. Another client whose member picked
macOS shows the microphone disabled, with a pointer to Settings. A member cannot
pick macOS on the desktop and a network service elsewhere in this PR.

## Storage

One private folder per member under the data directory:

```
service-connections/<user_id>/settings.json          {"dictation": "system" | <connection id>}
service-connections/<user_id>/connections/<id>/connection.json
service-connections/<user_id>/connections/<id>/key
```

- Written with the private atomic helper the provider credential store uses
  (`_write_private`), folders `0700`.
- `service-connections` joins `BACKUP_APP_DATA_EXCLUSIONS` and
  `TRANSFER_APP_DATA_EXCLUDED_ROOTS`. A restore drops it, so members connect
  again and fall back to macOS. No SQLite table, so no migration, restore
  fingerprint, or transfer table entry.
- **Not provider logins.** It shares nothing with `ProviderCredentialStore`'s
  account gate, generation, or readiness. A transcription key belongs to a
  person, not an execution account.
- **One writer per member.** Every write to a member's folder (Connect,
  Disconnect, the selection, and later voice settings) holds one per-member
  lock and rechecks live membership, and for a selection that the connection
  still exists, before writing. The lock also makes `_write_private`'s fixed
  temporary filename safe.
- **Member removal.** `MemberRemovalCoordinator`
  takes the same lock and deletes the member's folder idempotently after
  `begin_member_removal` commits, and again before `complete_member_removal`,
  so an interrupted removal resumes the cleanup. The folder stays out of the
  removal preview's hash.

## Contracts

All routes act for the authenticated member (the owner in a personal space),
and use a route class like `ProviderAccountRoute` so a validation error never
echoes the submitted key.

- `GET /api/service-connections` returns
  `{dictation: "system" | id, connections: [{id, kind, preset, label,
  base_url, model, formats, verified_at}]}`. Never a key.
- `POST /api/service-connections` takes `{kind: "openai_compatible" |
  "gemini", preset: "openai" | "groq" | "custom" | null, base_url, model,
  key}`. The backend owns preset URLs; `base_url` is accepted only for
  `custom`. The key is capped in length in `limits.py`. Runs the connect check;
  saves on success and returns the connection.
- `DELETE /api/service-connections/{id}` disconnects. If it was selected, the
  selection returns to `system`.
- `PUT /api/service-connections/selection` takes `{dictation: "system" | id}`.
- `POST /api/service-connections/{id}/transcribe` takes one raw audio body and
  returns `{text}`. The client captures the connection id when recording
  starts, so a Settings change on another client cannot redirect the audio. A
  disconnected id is refused.

Error codes: `connection_not_found` (404), `connection_check_failed` (422,
with a sanitized service message), `audio_too_large` (413),
`audio_type_unsupported` (415), `transcription_busy` (429),
`transcription_upstream_failed` (502, sanitized), `address_not_allowed` (422).
A sanitized message has the submitted key removed and is capped in length.

`formats` holds full MIME strings with codecs, for example
`audio/webm;codecs=opus` and `audio/mp4;codecs=mp4a.40.2`. The client passes
them unchanged to `MediaRecorder.isTypeSupported` and `MediaRecorder`, and sends
the chosen one as the upload's `Content-Type`. The web client sets that header
itself, because `api()` forces JSON on Blob bodies.

### Native dictation events

- `rcp://dictation-state` gains state `preparing` (the macOS 26 model download)
  and an `engine` field on `recording`: `speech_analyzer` or `apple_server`.
- Every `rcp://dictation-result` carries the session's whole text so far, as
  today: the composer replaces the span with it. On SpeechAnalyzer the native
  side accumulates finalized segments plus the current volatile text itself.
- `desktop_stop_dictation` gains a `finish` argument, and
  `stopDesktopDictation(sessionId, {finish})` passes it. The member's Stop sends
  `finish: true`: the native side sends the final result, then `stopped`.
  Invalidation (typing) sends `finish: false`: the native side cancels and sends
  `stopped` with no further result. Today Stop always cancels; both engines get
  the new behavior.

## Upload bounds

The transcribe route reads the body itself instead of using `UploadFile`,
because Starlette spools multipart parts over 1 MiB to disk. It requires
`Content-Length`, refuses a body over the limit before reading, counts the bytes
it actually receives, and enforces a read deadline and a small per-member
concurrency limit. Audio lives only in memory and is dropped when the request
ends. The byte cap is the only size rule; the backend does not parse durations.

The team middleware's media-type allowlist adds the audio types for this one
route only, and keeps the origin check. The personal loopback server needs no
new check: an `audio/*` request from another site needs a CORS preflight, which
the server refuses, and a no-cors request loses its type and gets 415. The
projections spec says so when this lands.

New limits in `limits.py`: maximum audio bytes (start at 4 MiB), key length,
outbound total deadline, outbound response bytes, and per-member concurrent
transcriptions.

## Outbound calls

- Connect to the address that was checked, without redirects or environment
  proxies, as `web_push.py` does.
- Read the response streamed and size-capped, under one total deadline, as
  `release_check.py` reads with `client.stream`. `web_push.py` does neither:
  it buffers the whole body, and its timeouts are per phase.
- Plain `http` only to a literal loopback address or `localhost`. Refuse a base
  URL with userinfo, a query, or a fragment.
- Gemini's key goes in the `x-goog-api-key` header, never in the URL.
- No SSRF fence beyond that: members are already trusted with the service
  account (see the
  [member terminal decision](../decisions/2026-09-19-a-member-terminal-inherits-the-work-trust-boundary.md)).

## Connect check

- Transcribe two bundled clips of about one second each, one WebM/Opus and one
  MP4/AAC, with the chosen model. Success is a 2xx with a parseable body, not
  non-empty text.
- The clips are recorded from real `MediaRecorder` output (Chrome WebM, WKWebView
  and Safari fragmented MP4), not made with ffmpeg, so a pass predicts a real
  upload.
- Save only if at least one passes; `formats` records which. A key that fails
  both is not saved.
- The clips live under `src/rcp/`, where `packaging/rcp_backend.spec` already
  bundles package data.

## Composer

- One web module owns the microphone. Dictation and, later, a voice session
  never hold it at the same time. The native recognizer counts as holding it.
- Recording uses `MediaRecorder`, with the first type in the connection's
  `formats` that `isTypeSupported` accepts. If none match, the button says which
  formats the service accepted.
- States: idle, starting, recording, transcribing, error. The returned text
  replaces the session's span in one step. Typing during transcribing
  invalidates the span, as it does today, and the late result is dropped.
- The transcribing state names the service, for example "Transcribing with
  Groq". On macOS, the recording state names the engine when it is
  `apple_server`.

## Settings

A **Transcription** card in Space Settings, beside Provider logins, labelled as
the member's own (the rest of Space Settings is space-wide):

- a service picker: macOS (desktop only) or a connection;
- one row per connection with model, accepted formats, last verified, and
  Disconnect;
- **Connect**, which opens a dialog: OpenAI, Groq, Gemini, or Custom server. It
  links to the service's API-key page, takes a masked key, a model (prefilled),
  and for Custom a base URL. The key is never shown back. The dialog says where
  audio goes and that RCP does not store it.

## macOS 26: SpeechAnalyzer

- On macOS 26 and later, macOS dictation uses `SpeechAnalyzer` with
  `SpeechTranscriber`, on-device, through the event contract above.
- If the locale's model is missing, the shell requests it through
  `AssetInventory` and reports `preparing`. macOS stores the model, not RCP, so
  the release does not grow by it.
- If `SpeechTranscriber` does not support the locale, the shell uses the old
  recognizer and reports engine `apple_server`, which the composer labels.
- macOS 13 to 15 keep `SFSpeechRecognizer`. Its
  `requiresOnDeviceRecognition = NO` allows audio to go to Apple. The speech
  usage string in `Info.plist` and `Info.dev.template.plist` describes each
  recognizer, not each macOS version: SpeechAnalyzer runs on the Mac; the older
  recognizer, used before macOS 26, for unsupported languages, and in builds
  without SpeechAnalyzer, may send audio to Apple.
- **Build.** SpeechAnalyzer is a Swift API (`public actor SpeechAnalyzer`).
  `build.rs` uses `cc::Build`, which cannot compile Swift, so it also runs
  `swiftc` on one new Swift file that exposes C entry points with `@_cdecl`,
  targeting `arm64-apple-macos13.0`, with the Swift library search paths,
  `-rpath /usr/lib/swift`, and Swift's compatibility libraries. Every new API
  sits behind `@available(macOS 26, *)`.
- **Toolchain.** End users install nothing; they get the prebuilt app.
  Building with SpeechAnalyzer needs the macOS 26 SDK. Command Line Tools for
  Xcode 26 provide it (about 0.9 GB from Software Update, no Apple ID); full
  Xcode is not needed. `docs/install.md` gains that line. GitHub's `macos-15`
  image has Xcode 26.0 to 26.3 installed but defaults to 16.4.
  `build-desktop.yml` selects Xcode 26.3; `desktop.yml` moves from
  `macos-latest` to `macos-15` and selects it too.
- **Older SDK.** `build.rs` reads the SDK version. Below 26 it skips the Swift
  file and prints a Cargo warning, and that app uses the old recognizer on
  every macOS. Release workflows set a variable that turns the skip into a
  build failure, so a release always includes SpeechAnalyzer.
- **Older-Mac guard.** In `build-desktop.yml`, the only workflow that builds
  the app bundle, a step fails if any macOS 26 Speech symbol is a strong import
  or any Swift overlay library is a non-weak `LC_LOAD_DYLIB`. One manual launch
  on macOS 13 to 15 closes the handoff.
- **Native test.** A Rust test owns the state and engine mapping in
  `dictation.rs`, as the projections spec requires for shell dictation.
- **Size.** The PR reports the app size before and after.

## Slices

Three slices run in parallel against the contracts above.

1. **Backend** (Python): storage, both adapters, connect check and clips,
   routes, upload bounds, outbound rules, middleware allowlist, limits, backup
   and transfer exclusion, member removal.
2. **Web** (TypeScript): Transcription card and dialog, microphone owner,
   `MediaRecorder` recording, format choice, composer states, the new native
   event fields.
3. **Native** (Swift, Objective-C, Rust, CI): SpeechAnalyzer path, asset
   preparation, finish-then-stop, engine reporting, `build.rs` with `swiftc`,
   older-SDK skip, CI Xcode selection, weak-link check, plist strings, the Rust
   test. The local toolchain is ready (macOS 26.5 SDK).

Probe first, at the start of each slice:

- the exact Gemini 3.5 Transcribe model id and which audio types
  `generateContent` accepts (WebM/Opus may not be one);
- which `MediaRecorder` types the desktop WKWebView, Chrome, and iOS Safari
  produce; record the check clips from them;
- that `/Applications/Xcode_26.3.app` on the `macos-15` image builds the Swift
  file with the deployment target at 13.0.

## Tests

One test per invariant, no wording assertions:

- a key is never in a response, a 422 body, a log, or a backup capture; the
  folder is private;
- a sanitized upstream error never contains the submitted key;
- a connection is saved only after a passing check, with `formats` recorded;
- a base URL outside the address policy is refused before any connection, and
  redirects are not followed;
- an oversized or trickling upstream response is cut off at its bound;
- the transcribe route refuses a missing or oversized `Content-Length`, a body
  that exceeds the limit while streaming, a wrong media type, and a
  disconnected id;
- the team middleware admits audio only on the transcribe route;
- member removal deletes the member's folder, a Connect or selection write
  that finishes after removal began does not recreate it, an interrupted removal
  finishes the cleanup, and a selection racing Disconnect never points at a
  missing connection;
- each adapter builds the request its service expects (mocked transport);
- the composer drops a late result after typing (node test);
- the native state and engine mapping (Rust test).

## Docs to update when this lands

- Conversations spec: the dictation paragraph (services, no storage, may-send
  wording for the old recognizer).
- API, Web, and desktop projections spec: the routes, clients, the personal
  loopback reasoning, and the SpeechAnalyzer path.
- Server and machine operations spec: the backup and transfer exclusion and
  member removal.
- `docs/install.md`: Command Line Tools for Xcode 26 for source desktop builds.
