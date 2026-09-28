# Show every artifact, and keep commenting separate

Status on 2026-09-27: design settled with a Codex xhigh review; the human
chose not to review it and asked to implement it in this pull request.

- Implemented (backend): changes 1–5, the backend half of 7 (saved-artifact
  capabilities), 8, the prompt lines of 9, and 10, with their Python tests.
- Implemented (web and desktop): the desktop PDF command in 4, chat cards in
  6, the web contracts, Artifacts inventory, and WebMCP in 7, with browser,
  TypeScript, and Rust coverage. The current behavior specs in 9 are updated.
- Verified: web build and full web suite, focused backend tests, Rust tests and
  clippy, native development bundle, frozen-backend build and smoke check, and
  the disposable served-app mixed-artifact journey. Chromium still reports the
  existing unsupported `navigate-to` CSP directive; no requests or page scripts
  failed in that journey.
- Remains: live native verification and human merge. The native PDF/viewer
  journey remains unverified: desktop startup fixes its backend port at 8421
  and initializes the normal app profile, so it cannot honor this slice's
  disposable-data and spare-port constraints without a separate launch change.
- Settled (human, 2026-09-27):
  - Viewing is universal. Every file an agent leaves in its turn artifact
    directory gets a card with Download and Keep, within the existing bounds.
  - Viewing and commenting are separate code paths. A new viewable type never
    touches comment code. A comment change never touches a per-type viewer.
  - Only the six current types (HTML, PNG, JPEG, GIF, WebP, SVG) support
    select-and-comment and revise.
  - PDF opens in the system PDF viewer in the desktop app.
- Settled (Codex review, 2026-09-27):
  - In a browser a PDF is Download only. Serving it inline would be a new
    containment contract that no source guarantees across browsers.
  - Notebooks show as pretty-printed JSON, not rendered cells.
  - Dotfiles are ordinary files. Empty files are skipped and reported.
  - Omissions are a projection of the discovery receipt, not a stored field.
  - The viewer shell has no Download button; the card owns Download.
- Closure: the pull request merges, verification passes, the spec sentences
  in change 9 land, and this handoff is deleted.

## Why

Discovery accepts six suffixes (`ARTIFACT_MEDIA_TYPES` in
`src/rcp/artifacts.py`). `_discover_chat_artifacts` (`src/rcp/runs/chat.py`)
skips every other file and only counts it as `unsupported_type` in a receipt
the human never sees. An agent that writes `results.csv`, `notes.md`, or
`paper.pdf` produces nothing visible. The spec says so on purpose.

Keep repeats the six suffixes by hand in the local state workspace and in three
shipped remote scripts.

Viewing and commenting are tangled:

- `artifact_viewer_document` builds two whole shells, read-only and with the
  selection rail, each with its own preview markup and the image `#boxLayer`.
- `artifact_viewer.js` is almost all selection and Add-to-chat logic.
- `html_preview_document` injects `artifact_selection.js` and the selection
  relay into every HTML sandbox, even with no chat.
- `can_discuss` and `can_revise` (`_agent_artifact_response` in
  `src/rcp/api/tasks.py`) know nothing about type.
- The task viewer refuses to render at all when the chat is missing
  (`_artifact_viewer_response`), though viewing does not need a chat.

## Target behavior

| File | Card | Open | Select + comment, revise |
|---|---|---|---|
| `.html`, `.htm` | yes | sandboxed viewer (unchanged) | yes |
| `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg` | yes, thumbnail when small | image viewer (unchanged) | yes |
| `.md`, `.markdown` | yes | rendered Markdown, script-free | no |
| Text: `.txt`, `.log`, `.csv`, `.tsv`, `.json`, `.jsonl`, `.yaml`, `.yml`, `.toml`, `.ipynb`, common code suffixes | yes | escaped text with line numbers, script-free | no |
| `.pdf` | yes | desktop: system PDF viewer; browser: none | no |
| Anything else, or bytes that fail their type check | yes | none | no |

Every card has Download and Keep. An empty file gets no card and is reported.

## Design

### 1. One type registry and a soft classifier

In `src/rcp/artifacts.py`:

- One registry maps a lowercase suffix to a media type. The view kind is
  derived from the media type in code and never persisted:

  ```python
  ArtifactView = Literal["html", "image", "markdown", "text", "pdf", "file"]
  ```

- `ArtifactMediaType` gains `text/markdown`, `text/plain`, `text/csv`,
  `application/json`, `application/pdf`, and `application/octet-stream`.
  Logs, YAML, TOML, notebooks, and code are `text/plain`.
- `validate_artifact_bytes` becomes
  `classify_artifact_bytes(name, data) -> ArtifactMediaType`. It returns the
  suffix's type when the bytes pass that type's recognition check, and
  `application/octet-stream` for an unknown suffix or failed check. It still
  raises for programming, filesystem, and resource errors. Checks: HTML
  UTF-8 without NUL; the raster signatures; SVG as UTF-8 XML with an `svg`
  root; Markdown and text as strict UTF-8 without NUL; PDF starting `%PDF-`.
- `descriptor_for(scope_id, name, *, media_type, …)` requires the media type.
  Scope checks call `artifact_id(scope_id, name)` directly.
- Loading (`_load_agent_artifact`): a stored `application/octet-stream`
  artifact stays a plain file forever. It is never promoted by current bytes
  or a newer registry. A stored typed artifact must still classify to its
  stored type before it is interpreted; otherwise `410`, as today.
- HTML-only callers keep an explicit check on the classifier result: episode
  reports (`runs/tasks/episode_report.py`), legacy result views
  (`runs/tasks/result_views.py`, `storage/models.py`
  `_validated_result_view_html`, `api/result_views.py`).
- Revision candidates keep their source media type (raster and SVG revisions
  keep working). `stage_artifact_context` and `finalize_artifact_revision`
  require a commentable stored type (change 3).
- `size_bytes` stays `ge=1`.

### 2. Discovery and omissions

`_discover_chat_artifacts` considers every direct regular child. No suffix
filter, no dotfile filter, no recursion; symlinks and directories stay out.

- Limits stay: `CHAT_ARTIFACT_MAX_COUNT = 8` read attempts,
  `CHAT_ARTIFACT_MAX_FILE_BYTES = 16 MiB`, and
  `CHAT_ARTIFACT_MAX_TOTAL_BYTES = 32 MiB` attached. They bound one turn's
  discovery, not repeated downloads or rendered size.
- Check the advertised size and the total before spending a read attempt, so
  oversized early files do not crowd out later ones.
- An empty file is omitted with reason `empty`.
- Reasons: `count_limit`, `file_size_limit`, `total_size_limit`, `empty`,
  `invalid_or_unavailable`. `unsupported_type` disappears. Discovery failure
  (`discovery_unavailable`) is not a file count.
- Omissions are projected, not stored. The task projection gains
  `result.artifact_omissions`, read from the latest `artifact_discovery`
  receipt:
  - Discovery receipts move from diagnostic to summary retention and join
    `_PROTECTED_AGENT_TASK_RECEIPT_CATEGORIES` (`storage/agent_tasks.py`), so
    later summary receipts of a long turn cannot prune them.
  - Read them with one batched, category-specific query for the projected
    tasks.
  - Deduplicate against the latest receipt only, not any earlier identical
    one, so the latest outcome always wins.
  - Project only known reason keys with non-negative counts, plus a
    `discovery_failed` flag. Never paths or exception text.
- A revision turn no longer suppresses its other outputs. The exact
  replacement stays the candidate on the original card. Every other file in
  that turn becomes an ordinary card.

### 3. The seam

Two modules. The view side never imports the comment side. Routes compose.

**View side** (every type):

- `src/rcp/artifacts.py`: descriptor, identity, registry, classifier, bounded
  file I/O, the HTML sanitizer, and `html_preview_document`.
- New `src/rcp/artifact_views.py`:
  - `artifact_viewer_document(descriptor, *, content_url, state,
    keep_url=None, save_url=None, panel=None)`. One shell: header (name, state,
    Save copy, Keep) and one preview for the view kind. It takes no chat,
    operation, episode, or branch argument. It owns one CSP; it omits
    `script-src` when it emits no script.
  - Preview markup per view kind inside one positioned container: `#preview`
    (iframe) or `#previewImage` (image).
  - `markdown_document(data)` and `text_document(name, data)` for `/content`.
- `html_preview_document(data, *, frame_addon=None,
  result_view_gestures=False)`. Without an addon the sandbox carries only the
  reference relay. The selection bootstrap and relay leave this function.
- Trusted internal fragments, not a plugin framework:

  ```python
  ViewerPanel(markup, style, script)
  FrameAddon(frame_script, wrapper_script)
  ```

- The addon contract: `frame_script` runs inside the private closure before
  agent scripts; `wrapper_script` installs its listeners before the child frame
  starts; both receive the private channel after the handshake and own their
  own enable and clear state. Existing source, handshake-shape, version, and
  trusted-gesture checks stay. No port or helper becomes an agent-visible
  global.
- `src/rcp/repository_preview.py` and `artifact_views.py` share one neutral
  escaped-line helper (text, start line, optional selected line). Repository
  loading, window labels, and its CSP (`frame-ancestors 'none'`) stay in
  `repository_preview.py`.

**Comment side** (the six types only):

- New `src/rcp/artifact_comments.py`:
  - `COMMENTABLE_MEDIA_TYPES` and `supports_comments(media_type)`.
  - `comment_panel(config) -> ViewerPanel`: Selections rail, Add to chat,
    Open chat, and the draft store. `config` holds project, chat, operation,
    source, episode, and branch.
  - `selection_frame_addon() -> FrameAddon`: `artifact_selection.js` plus the
    selection relay taken out of `html_preview_document`.
  - #206's crop helper, once both land.
- `artifact_viewer.js` becomes `artifact_comment_panel.js`. It creates and
  removes its own image overlay in the preview container, sized to the
  letterboxed image, so the shell no longer writes `#boxLayer`.
- `artifact_selection.js` is unchanged and loaded only by this module.
- `packaging/rcp_backend.spec` and `packaging/hooks/validate_frozen_resources.py`
  follow the rename.

**Composition** (`src/rcp/api/tasks.py`, `src/rcp/api/episode_routes.py`):

```python
panel = comment_panel(config) if projected.can_discuss else None
document, csp = artifact_viewer_document(projected, ..., panel=panel)
```

- Task HTML `/content` gets `selection_frame_addon()` only when that artifact
  can be discussed. Report content uses its originating chat's eligibility.
  Revision comparison and result-view content get no comment addon; result
  views keep `result_view_gestures` unchanged.
- A missing chat removes the panel. It no longer refuses the viewer.

**Projections and gates:**

- `can_discuss` requires `supports_comments(media_type)`; `can_revise`
  follows it.
- `_admit_artifact_context_request`, `stage_artifact_context`, and revision
  finalization refuse non-commentable types. The projection is only an offer.
- `AgentArtifactResponse` gains `view`. `can_open` means "has an RCP viewer",
  so it is false for `pdf` and `file`. The desktop app offers the system PDF
  viewer when `view === "pdf"` and `can_download`.

After this, a new viewable type touches the registry, the classifier, and one
renderer in `artifact_views.py`. A comment change touches
`artifact_comments.py`, its two scripts, and the selection models in
`service.py`.

### 4. Routes

`/viewer` (task and report): the one shell. Not used for `pdf` or `file`.

`/content`, by view kind:

- `html`: `html_preview_document` (sandbox and CSP unchanged).
- `image`: raw bytes with `default-src 'none'; sandbox` (unchanged). The
  `/preview` image-`Accept` compatibility stays image-only.
- `markdown`: `markdown-it-py` (new direct dependency) as
  `MarkdownIt("commonmark", {"html": False})`, no linkify, no plugins.
  Renderer rules, not regexes, make links render as escaped label plus URL
  with no `href`, and images as escaped alt text plus URL with no `<img>`.
  Raw HTML and math stay visible source.
- `text`: escaped lines with numbers. `.json` and `.ipynb` are pretty-printed
  (keys unsorted) when they parse and fit the budget; otherwise raw.
- Render budget: the first `ARTIFACT_PREVIEW_MAX_BYTES = 2 MiB` and
  `ARTIFACT_PREVIEW_MAX_LINES = 10_000` lines, both in `limits.py`, applied
  before parsing. A truncated preview says so; Download has the whole file.
- Markdown and text documents are RCP-generated `text/html` with a header CSP
  `sandbox; default-src 'none'; style-src 'unsafe-inline'; base-uri 'none';
  form-action 'none'; frame-ancestors 'self'`. The shell embeds them directly
  in `<iframe sandbox>` with no tokens.
- `pdf` and `file`: `404`. Download serves them.

`/download`: every kind, unchanged headers (`attachment`, `nosniff`,
`default-src 'none'; sandbox`), source media type. The ASCII fallback filename
keeps any short `[a-z0-9]{1,16}` suffix.

**Desktop PDF open:** new Tauri command
`open_artifact_pdf(project_id, task_id, artifact_id)`.

- It builds the `/download` URL itself and fetches through `ResourceAccess`.
  It checks a success status, `application/pdf`, a bounded size, and the
  `%PDF-` prefix.
- It writes `artifact.pdf` with private permissions into a fresh private
  directory under an app-owned cache folder, and opens it with
  `tauri-plugin-opener`. It is never a general file or URL opener.
- Retention: a failed open removes its directory at once. Successful opens are
  kept, and directories older than `PDF_PREVIEW_RETENTION` (one day) are
  removed on startup and on each open.
- Registered in `build.rs`, `lib.rs`, main-window capabilities only, and
  `desktopRuntime.ts`. Preview windows gain nothing.

### 5. Keep and kept storage

- One kept-name grammar,
  `[a-z0-9][a-z0-9-]{0,220}(?:\.[a-z0-9]{1,16})?`, with the existing slug,
  date, and collision scheme. A long, odd, or missing suffix gives an
  extensionless kept name instead of a refusal.
- Update every artifact gate: `transport/state.py` (pattern and
  `_artifact_base_name`), `transport/remote_read_kept_view.py`, and
  `transport/remote_lock_holder.py` (restore, Keep publication, replacement).
  Legacy `views/` stays HTML-only.
- Reads classify by the descriptor's original name, not the kept filename.

### 6. Chat card

`web/src/components/NodeChat.tsx`:

- Every projected artifact renders a card: name, size, Download, and Keep
  (the existing POST, then refresh the task projection).
- Open when `can_open`. In the desktop app a `pdf` card with `can_download`
  also shows Open, which calls the new command.
- Thumbnail when `view === "image"`, replacing `media_type !== "text/html"`.
- Preview failure is its own state. A broken thumbnail or popup no longer
  hides Download or Keep; `can_download` and `can_keep` govern those.
- An omission line under the cards, also for a turn with no cards: counts by
  reason, or "discovery failed" with no count.
- An answer link to a download-only artifact resolves to its card.

### 7. Web contracts and the Artifacts tab

- `web/src/types.ts`: `ArtifactView`, the wider media union, omissions on
  the task result, and wider `ProjectArtifact`.
- `web/src/agentTasks.ts`: validate `view` and omissions; carry omissions
  through reconstruction and history reconciliation, including omission-only
  turns.
- `SavedArtifactResponse` (`src/rcp/api/artifacts.py`) gains `view`,
  `available`, `can_download`, and a nullable `download_url`; `can_open` is
  projected, not defaulted. Reports keep their viewer and Save copy.
- `web/src/views/Artifacts.tsx`: a download-only entry shows Download, not
  "Preview unavailable".
- WebMCP (`webmcp.ts`, `App.tsx`): list plain files with `can_open=false`,
  `can_download=true`, and `view`; refuse visual opening for them.

### 8. Security invariants

- Invariant 10e holds. Agent frames never gain `allow-same-origin`, popup,
  form, or download tokens. Reference relay and the self-navigation caveat
  stay. No agent code moves into the trusted shell.
- Agent bytes are interpreted as HTML only for view `html`. Markdown and text
  `/content` are RCP-generated, escaped, script-free, and sandboxed.
- Normal SVG display uses `<img>`. Direct and comparison `/content` keeps its
  no-script sandbox CSP.
- Every response is `nosniff`. `/content` for Markdown and text is RCP
  `text/html`; Download uses the stored source type.
- Comment scripts are injected only when the artifact can be discussed.
- PDFs never load into an RCP webview or an RCP-origin browser tab.

### 9. Docs and prompts

- `docs/specs/paper-artifacts-and-result-views.md`: discovery keeps every
  bounded direct regular child; omissions are reported; view kinds and their
  rendering promise; only the six types support comment and revise; a revision
  turn's other outputs are cards; PDF paths.
- `docs/specs/api-web-and-desktop-projections.md`: `view`,
  `artifact_omissions`, `/content` by kind, refusal of artifact context for
  other types, saved-artifact capabilities, the desktop command.
- Provider prompts that promise only HTML and image discovery
  (`agents/prompts.py`, `agents/experiment_loop_prompt.py`).

### 10. Readers never block on a swapped file

`read_local_regular_file` (`src/rcp/artifacts.py`) and the shipped remote
reader in `src/rcp/transport/run_stage.py` open the file before `fstat`
proves it is regular. A regular file replaced by a FIFO after listing blocks
that open forever. Both open with `O_NONBLOCK` added and keep the existing
regular-file check, which then refuses the FIFO at once. Regular files ignore
the flag, so reads are unchanged. Discovery now reads every file type, which
makes this path more reachable.

## Landing after #206

#206 changes the comment path: raster box selections get a server
crop, and HTML boxes name their elements (`elements` on
`ArtifactBoxSelection`). Both are comment-side. The view layer passes
selection payloads through without a field allowlist. Whichever lands second
puts the crop helper in `artifact_comments.py`, and the comment panel owns the
one definition of box coordinates over a letterboxed image.

## Verification

Python (`uv run pytest -n0 …`), behavior only, no wording:

- Classification and loading: unknown suffix, bad known-suffix bytes, empty
  file omitted, pinned octet-stream, changed typed bytes refused, the six
  comment types still discussable, context POSTs refused for other types.
- Discovery: mixed-type limits, size checked before a read attempt,
  omissions on a turn with no cards, latest receipt wins, revision turn's
  other outputs, the answer survives discovery failure.
- Content: Markdown and text have no script element, a `sandbox` CSP without
  `allow-scripts`, escaped source markup, no link `href` or `img`, and a
  truncation marker past the budget. `pdf` and `file` content is `404`.
  Download is `attachment` for every kind. A viewer with no chat renders
  without a panel. HTML without discussion carries no selection script.
- Storage: local and remote Keep, read, and restore for `.csv`, an
  extensionless name, and a long suffix; collisions; transfer and backup.
- Existing `test_unified_artifacts.py`, `test_result_view_artifacts.py`,
  `test_saved_artifacts_api.py`, `test_history_only_tasks.py`, and the
  transfer tests updated.

Web:

- `web/tests/artifacts.browser.test.mjs`: a file card has Download and Keep
  and no Open; a text card opens a viewer with no Selections panel; HTML and
  PNG still have it; a broken thumbnail keeps Download.
- `web/tests/artifactSelection.browser.test.mjs` loads documents from the
  real renderer and routes, not regexes over Python source.
- `npm --prefix web run build`; the Rust tests for the new command.

Served-app journey (throwaway `RCP_DATA_DIR`, spare port, never 8421): seed a
chat turn with `.html`, `.png`, `.md`, `.csv`, `.pdf`, `.bin`, and an empty
file. Check the cards and omission line, open Markdown and CSV, download the
`.bin` and the PDF, Keep the CSV and find it in the Artifacts tab, and see the
Selections panel on HTML and PNG only. Read CSP headers, console, network,
and server logs.

Desktop: rebuild Tauri and the frozen backend; open a PDF in the system
viewer; Markdown and text open in the preview window; Download still works.
