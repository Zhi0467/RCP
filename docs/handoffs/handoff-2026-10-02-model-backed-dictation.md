# Model-backed dictation

Date: 2026-10-02
Status: design settled with the human on 2026-10-02 (issue #229, part 1).
Nothing is implemented yet. The standby voice agent (part 2) is a separate
handoff and PR; it reuses this PR's service connections and microphone owner.

Close this handoff when all of these hold on real hardware:

- the desktop app in a personal space dictates through a connected OpenAI or
  Groq key, and the text lands in the editable span;
- a team member dictates the same way from the browser app and from the phone
  web app, each with their own key;
- a Gemini connection dictates from at least one client;
- on macOS 26 or later, macOS dictation runs on-device through SpeechAnalyzer,
  including the first-use model download;
- the release candidate launches and dictates on a Mac running macOS 13, 14,
  or 15, through the old recognizer.

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
- **macOS 26 moves to SpeechAnalyzer in this PR.** Older macOS keeps today's
  recognizer.
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

Each member picks one dictation service in Settings: **macOS** or one of their
connections. A new member starts on macOS. The desktop app honors either
choice. Another client whose member picked macOS shows the microphone disabled,
with a pointer to Settings. A member cannot pick macOS on the desktop and a
network service elsewhere in this PR.

## Service connections

A connection is `{id, kind, label, base_url, model, formats, verified_at}` plus
a secret key, owned by one member.

- **Storage.** Under the data directory, one private folder per member and
  connection: a nonsecret JSON record and a key file, written with the same
  private atomic helper as the provider credential store. The folder is listed
  in `BACKUP_APP_DATA_EXCLUSIONS`, so a restored server asks members to connect
  again. `rcp server member remove` deletes the member's folder. Project
  transfer never carries it.
- **Separate from provider logins.** It is not `ProviderCredentialStore` and
  shares none of its account gate, generation, or readiness. A transcription key
  belongs to a person, not an execution account.
- **Key never leaves the backend.** No response, log, event, or backup contains
  it. The desktop Keychain helper is not used (it caps values at 64 bytes, and
  the web and phone clients have no Keychain).
- **Key bound to its origin.** A connection's base URL cannot change; connect a
  new one instead. There is no edit route in this PR: Disconnect, then Connect.
- **Verified before it is saved.** Connect transcribes two bundled clips of
  about one second each, one WebM/Opus and one MP4/AAC, with the chosen model.
  The connection is saved only if at least one succeeds. `formats` records which
  ones did. A key that fails both is not saved, and the dialog shows the
  service's own error.
- **Transport.** Outbound calls reuse the bounded `httpx` pattern of
  `web_push.py`: no redirects, no environment proxies, bounded timeouts and
  response size. The address policy above is checked before every call. No
  SSRF fence beyond that: members are already trusted with the service account
  (see the [member terminal decision](../decisions/2026-09-19-a-member-terminal-inherits-the-work-trust-boundary.md)).

## Routes

All routes act for the authenticated member (the owner in a personal space).

- `GET /api/transcription` returns the member's selected service and their
  connections, without keys.
- `POST /api/transcription/connections` takes `{kind, preset, base_url, model,
  key}`, runs the connect check, and saves on success.
- `DELETE /api/transcription/connections/{id}` disconnects. If it was
  selected, the selection returns to macOS.
- `PUT /api/transcription/selection` takes `{selected: "system" | <id>}`.
- `POST /api/transcription/transcribe` takes one raw audio body
  (`audio/webm` or `audio/mp4`), sends it to the selected connection, and
  returns `{text}`.

The transcribe route reads the body itself instead of using `UploadFile`,
because Starlette spools multipart parts over 1 MiB to disk. It requires
`Content-Length`, refuses a body over the limit before reading, counts the bytes
it actually receives, and enforces a read deadline and a small per-member
concurrency limit. Audio lives only in memory and is dropped when the request
ends. The team middleware's media-type allowlist adds these two audio types for
this one route only, and keeps the origin check.

New limits in `limits.py`: maximum audio bytes (start at 4 MiB), maximum
segment length (55 s, the composer's existing cap), outbound timeout,
per-member concurrent transcriptions, and maximum response size.

## Composer

- One web module owns the microphone. Dictation and, later, a voice session
  never hold it at the same time. The native recognizer counts as holding it.
- Recording uses `MediaRecorder`. The client picks the first type in the
  connection's `formats` that `MediaRecorder.isTypeSupported` accepts. If none
  match, the button explains which formats the service accepted.
- States: idle, recording, transcribing, idle. The returned text replaces the
  session's span in one step. Typing during transcribing invalidates the span,
  as it does today, and the late result is dropped.
- The transcribing state names the service, for example "Transcribing with
  Groq".

## Settings

A **Transcription** card in Space Settings, beside Provider logins:

- a service picker: macOS (desktop only) or a connection;
- one row per connection with model, accepted formats, last verified, and
  Disconnect;
- **Connect**, which opens a dialog: OpenAI, Groq, Gemini, or Custom server. It
  links to the service's API-key page, takes a masked key, a model (prefilled),
  and for Custom a base URL. The key is never shown back. The dialog says where
  audio goes and that RCP does not store it.

## macOS 26: SpeechAnalyzer

- On macOS 26 and later, macOS dictation uses `SpeechAnalyzer` with
  `SpeechTranscriber`, on-device. Its volatile results feed the existing
  `rcp://dictation-result` events, so the composer keeps live text.
- If the locale's model is missing, the shell requests it through
  `AssetInventory` and reports a "Preparing speech model" state. macOS stores
  the model, not RCP, so the release does not grow by it.
- If `SpeechTranscriber` does not support the locale, the shell uses the old
  recognizer and the composer labels it "Apple server recognition". This is a
  visible fallback, not a silent one.
- macOS 13 to 25 keep `SFSpeechRecognizer` unchanged. Its
  `requiresOnDeviceRecognition = NO` means audio may go to Apple; the spec and
  the Info.plist usage string must say "may", not "on-device".
- **Build.** SpeechAnalyzer is a Swift API. A new Swift file exposes C entry
  points with `@_cdecl`, and `build.rs` compiles it beside `dictation.m`.
  Deployment target stays 13.0; every new API sits behind
  `@available(macOS 26, *)`, so its symbols are weak-linked.
- **Toolchain.** End users install nothing; they get the prebuilt app.
  Building with SpeechAnalyzer needs the macOS 26 SDK. Command Line Tools for
  Xcode 26 provide it (about 0.9 GB from Software Update, no Apple ID); full
  Xcode is not needed. `docs/install.md` gains that line. GitHub's `macos-15`
  image has Xcode 26.0 to 26.3 installed but defaults to 16.4, so
  `build-desktop.yml` and `desktop.yml` select Xcode 26.3.
- **Older SDK.** `build.rs` reads the SDK version. Below 26 it skips the Swift
  file and prints a Cargo warning, and that app uses the old recognizer on
  every macOS. Release workflows set a variable that turns the skip into a
  build failure, so a release always includes SpeechAnalyzer.
- **Older-Mac guard.** A CI step fails if any macOS 26 Speech symbol is a strong
  import in the built binary. One manual launch on macOS 13 to 15 closes the
  handoff (see above).
- **Size.** The PR reports the app size before and after. The Swift runtime
  ships with macOS, so only the new code is added.

## Slices

Three slices run in parallel against the contracts above.

1. **Backend** (Python): connection store, both adapters, connect check,
   routes, transcribe upload bounds, middleware allowlist, limits, backup
   exclusion, member removal.
2. **Web** (TypeScript): Transcription card and dialog, microphone owner,
   `MediaRecorder` recording, format choice, composer states.
3. **Native** (Swift, Objective-C, Rust, CI): SpeechAnalyzer path, asset
   preparation, visible fallback, `build.rs`, CI Xcode selection, weak-link
   check, older-SDK skip, Info.plist strings. Starts once Command Line Tools
   for Xcode 26 are installed locally.

Probe first, at the start of each slice:

- the exact Gemini 3.5 Transcribe model id and which audio types
  `generateContent` accepts (WebM/Opus may not be one);
- which `MediaRecorder` types the desktop WKWebView and iOS Safari produce;
- that `/Applications/Xcode_26.3.app` on the `macos-15` image builds the
  Swift file with the deployment target at 13.0.

## Tests

One test per invariant, no wording assertions:

- a key is never in a response, log, or backup capture; the folder is private;
- a connection is saved only after a passing check, with `formats` recorded;
- a base URL outside the address policy is refused before any connection, and
  redirects are not followed;
- the transcribe route refuses a missing or oversized `Content-Length`, a body
  that exceeds the limit while streaming, and a wrong media type;
- the team middleware admits audio only on the transcribe route;
- `rcp server member remove` deletes the member's connections;
- each adapter builds the request its service expects (mocked transport);
- the composer drops a late result after typing (node test).

## Docs to update when this lands

- Conversations spec: the dictation paragraph (services, no storage, may-send
  wording for the old recognizer).
- API, Web, and desktop projections spec: the routes, clients, and the
  SpeechAnalyzer path.
- Server and machine operations spec: the backup exclusion and member removal.
