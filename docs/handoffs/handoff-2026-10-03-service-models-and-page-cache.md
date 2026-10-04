# Service models in one card, and a page that never goes stale

Date: 2026-10-03
Status: design settled with the human on 2026-10-03. Not started; waiting for
an explicit start. One PR, branch `fix/team-page-cache-and-voice-models`.

## Problem 1: the desktop app keeps showing an old team page

The server sends `index.html` through `StaticFiles` with `Last-Modified` and
`ETag` but no `Cache-Control`. WebKit then picks its own freshness: 10% of the
file's age. A three-day-old page stays "fresh" for about seven hours, so after
a server update, re-entering the team space and reloading keep the old page
without asking the server. Seen on 2026-10-03: the app entered the team space
81 seconds before the server restarted on v0.4.10, and the voice button and
the Dictation and voice card stayed missing.

**Fix.** Every HTML response from the web mount (the `/` index and the SPA
entry) carries `Cache-Control: no-cache`, so WebKit revalidates with the ETag
on every load. Hashed `/assets/*` files are unchanged. The phone listener
already sends `no-store` for its page. Owner: `src/rcp/api/app.py` web mount.

## Problem 2: models are scattered, typed by hand, and unexplained

Today the Connect dialog has one free-text **Model** box for the dictation
model. The voice agent's delegation model is a second free-text box under
**Standby voice agent**, outside the connection. The live voice model is
fixed in code and never shown. Nothing says which model does what, and a
connected service shows only one model with no way to change it.

### Settled decisions

- **One card per connection holds every model it uses.** Connect opens the
  card; after saving, the same card opens again from the connection with
  **Edit**. The separate delegation-model form goes away.
- **Each model is a dropdown plus "Other…" for typing an id.**
- **The dropdowns list models live from the provider**, with the key just
  typed (Connect) or the stored key (Edit). A name rule picks the relevant
  ids, and ids with a `shutdown_date` are hidden. No handwritten model list.
- **Saving checks the choice for real**, as today: dictation transcribes the
  two bundled clips; the delegation model must answer `GET models/{id}`.

### Models per service

| Service | Dictation model | Voice agent models |
| --- | --- | --- |
| OpenAI | ids containing `transcribe` or `whisper` (excluding `diarize`) | Thinking model: ids `gpt-<digit>…` without `transcribe`, `tts`, `audio`, `realtime`, `live`, `image`, `search`; default `gpt-6-luna`. Voice: `gpt-live-1`, shown read-only (fixed in code). |
| Groq | ids containing `whisper` | — |
| Gemini | models whose `supportedGenerationMethods` has `generateContent`, ids containing `transcribe` first | — |
| Custom server | `GET {base}/models` if it answers; ids containing `transcribe` or `whisper` first, then the rest | — |

A row appears only for a purpose the connection has: an OpenAI dictation-only
key shows no voice rows, a voice-only key no dictation row.

When listing fails (bad key, unreachable, no `/models`), the dropdown shows
only **Other…** with a visible note saying the list could not be loaded and
why. Typing still works; the save check decides.

### API

- `POST /api/service-connections/models` takes `{kind, preset, base_url, key}`
  or `{connection_id}` (stored key) and returns
  `{transcription: [id], delegation: [id]}`. Same outbound rules as the
  connection check: bounded transport, no proxies or redirects, errors never
  echo the key. Nothing is stored.
- `POST /api/service-connections` (Connect) also accepts `delegation_model`
  when `purposes` includes `voice`.
- `PUT /api/service-connections/{id}` replaces `PUT /{id}/purposes`: it takes
  `{purposes, model, delegation_model}`, re-checks only what changed (a new
  dictation model reruns the clips; a new delegation model reruns the lookup),
  and saves under the existing member lock. The **Runs on** picker uses it too.
- The delegation model stays in the member's voice settings (only one
  connection holds `voice` at a time); Connect and Edit write it under the same
  lock as the connection. `PUT /api/voice/settings` keeps `confirm` only.
- The key is not editable. To change a key, disconnect and connect again.

### Web

- `ConnectServiceDialog` becomes the card for both Connect and Edit. On Edit the
  service and key fields are locked.
- One model field component: a `<select>` of listed ids plus **Other…**, which
  reveals a text input. Each field has a one-line label saying what it is for:
  "Turns your speech into text" for dictation; "Does the work you ask for"
  for the thinking model; "The voice you talk to" for the live model.
- Each connection in **Your services** lists its models with those labels and
  has **Edit** beside **Disconnect**.

### Not in scope

- Making `gpt-live-1` selectable (one model exists).
- Changing a key in place.
- An automatic page reload when the server updates under an open window.
  `no-cache` fixes re-entry and reload; a live banner is a separate change.

## Checks

- Python: API tests for the models route (filtering, `shutdown_date`, key
  never echoed, listing failure), Connect and Edit with `delegation_model`,
  Edit re-checking only changed models, and the index `Cache-Control` header.
  `uv run pytest -n0` on the touched test files and `ruff`.
- Web: helper tests for the name rules if they live in the Web; `npm --prefix
  web run build`.
- Served app on a throwaway server: Connect, Edit, and listing failure, with an
  HTTP double for the provider. One real OpenAI key run remains a live check.
- Desktop: after a server update, re-entering the team space shows the new page
  without quitting the app.

## Docs to update when done

`docs/specs/api-web-and-desktop-projections.md` (Voice agent and Dictation
sections). Delete this handoff in the same change.
