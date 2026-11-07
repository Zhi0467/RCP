# Extend the shared artifact preview to math and Paper assets

## Outcome

Extend the existing artifact renderer and Paper HTML preview. Markdown artifacts
and Paper HTML render the same supported math as chat. Paper HTML displays images
from the saved Paper tree and has document zoom beside Comment. Keep the existing
opaque sandbox and use no external math service or server-side Node process.

The work stays on the current feature branch and ships with the skill, relay,
question-binding, coach-picker and helper-script fixes in one PR.

## Confirmed failures and current owners

- `src/rcp/artifact_views.py` parses artifact Markdown with CommonMark but no math.
  Valid math remains literal and underscores can become Markdown emphasis.
- `web/src/paper/PaperPreview.tsx` already renders Markdown with chat's remark and
  KaTeX path. Its HTML preview already uses `inline_viewer_document` and the shared
  artifact sanitizer; changing viewer components would retain the failures.
- `src/rcp/artifact_preview.py` removes relative image and external script sources.
  Paper's HTML route does not resolve saved-tree images before sanitization.
- The HTML preview and shared shell have no document zoom controls.
- The inspected Project Agent's retained prompt includes inline syntax and
  in-place editing instructions. Its recent artifact-producing replies use no
  embed syntax. Its original note is writable inside the recorded grants, and
  inspected note-related tools report no error. No permission widening is needed.

## Shared math renderer

Create one browser renderer entry using the existing declared ReactMarkdown,
React server rendering, remark plugins, rehype-KaTeX and KaTeX dependencies.
Extract only shared syntax and math options from `chatMarkdown.ts` into a small
pure module. Chat, Paper Markdown and the artifact entry import that owner.
Do not import chat node links, glossary, artifact discovery or app actions.

The existing Vite build emits `artifact-renderer.js` (a self-contained IIFE)
and `artifact-renderer.css` (KaTeX styles with font URLs replaced by data URLs)
into `web/dist`. Create `web/build/artifactRenderer.ts`; its Vite plugin produces
these assets during `generateBundle` and registers the entry, shared syntax and
stylesheet/font inputs with `addWatchFile`. Do not add a second chained build, generated vendor files
to Git, a runtime Node requirement, a CDN, or new dependencies. Python reads the
installed resource through `web_dist_path()`, which already resolves source,
wheel and frozen assets. `src/rcp/artifact_renderer.py` loads those two files.
The IIFE defines `RcpArtifactRenderer` within the existing bootstrap's lexical
closure, exporting `renderMarkdown(source)` and `renderHtmlMath(element)`; it
creates no window global. A missing resource gives an explicit preview/build
failure rather than silently showing unrendered math.

For Markdown artifacts, render the original bounded source with the shared
Markdown parser before it can be damaged by CommonMark. Transport source as
escaped JSON/text, never as executable source or trusted HTML. ReactMarkdown
keeps source HTML inert. Preserve the current inert links and images, table/code
behavior, truncation notice and inline appearance. KaTeX uses `trust: false` and
the same syntax/error behavior as chat. Code remains literal and malformed math
stays visible. Plain-text viewers remain script-free.

`markdown_document` embeds escaped source JSON and calls `renderMarkdown` in
the trusted bootstrap. Its inert-link/image React components retain readable
labels and targets without loading or navigation. The panel iframe's sandbox
becomes `allow-scripts` for Markdown as it already is for inline Markdown; no
`allow-same-origin` is added. Markdown gets its own CSP: sandbox allow-scripts,
default-src none, inline scripts/styles, font-src data, no connect, frame, image,
form, object or base capability. Plain-text CSP remains unchanged.

The Markdown frame now executes RCP's renderer, replacing the old literal
script-free implementation. It remains opaque and cannot execute author HTML
or JavaScript, load network resources, fetch RCP, navigate the parent, open
popups or submit forms. Test these promises explicitly; do not preserve them
merely by assuming Markdown input is benign.

For Paper HTML, inline the same local KaTeX renderer through its existing
FrameAddon as a lexically scoped RCP addon inside the existing script-enabled
opaque frame. Generic authored HTML artifacts and the stdlib-only remotely
shipped artifact-preview CLI retain their existing self-contained-page contract;
this change does not add math-resource transport to that CLI. Use KaTeX's existing
`contrib/auto-render` export to traverse HTML text nodes on DOMContentLoaded.
Declare delimiters in this order: `$$...$$`, `\\[...\\]`, `\\(...\\)`, `$...$`;
the first two display, the latter two inline. Preserve auto-render's escaped
delimiter handling and unmatched text. Delimiters do not span HTML elements.
Ignore script, noscript, style, textarea, pre, code and option tags and existing
katex/katex-display classes. Share `strict: false`, `trust: false` and visible
malformed-expression behavior with Markdown. Do not replace author scripts or hide their errors. External dependencies
stay blocked. Font data and styles are self-contained. No mutation-observer or
dynamic-page math framework is part of this change.

## Saved Paper images

Add `html_preview_state` and `html_preview` read methods to PaperLive, following
`raw_file`'s ensure/worker/read-transaction pattern. A helper in
`src/rcp/paper/html_preview.py` uses one `PaperSnapshotReader` under explicit
`BEGIN`: capture manifest/revision once, read only the selected HTML body and
referenced image entries with `raw_file(..., revision)`. Do not call `snapshot()`
and eagerly load unrelated tree bodies. State reads need only image metadata;
content reads load admitted image bytes. This binds the HTML and images to one
exact saved revision, including an explicitly requested historical revision.
Resolve relative image references against the HTML's Paper-relative directory,
as Paper Markdown does. Look up normalized targets only in that snapshot;
never read arbitrary server paths or fetch URLs. Parent-directory segments are
allowed only when the normalized result remains inside the Paper tree.

Inline supported image bytes as data images before the existing sanitizer.
Support PNG, JPEG, GIF, WebP and SVG. For local references, parse URL components,
reject scheme/netloc/absolute paths, percent-decode the path once, normalize it
against the HTML's directory and reject NUL/backslashes or a normalized escape.
Ignore the query for saved-tree lookup, preserve an SVG fragment on the data URL,
and discard non-SVG fragments. Existing data/blob images retain their behavior.
Retain the
ordinary sanitizer for all other loading attributes. Do not grant general API,
filesystem, script, font or network access to authored HTML.

Set `PAPER_HTML_PREVIEW_MAX_ASSET_BYTES = PAPER_MAX_UPLOAD_BYTES` (16 MiB),
`PAPER_HTML_PREVIEW_MAX_ASSET_TOTAL_BYTES = PAPER_CHANGES_MAX_TOTAL_BYTES`
(32 MiB), and `PAPER_HTML_PREVIEW_MAX_OUTPUT_BYTES = PAPER_CHANGES_MAX_TOTAL_BYTES`
(32 MiB) in `limits.py`. Check metadata sizes and exact base64 expansion length
before loading/expanding. Count unique source bytes once, but every reference
against expanded output, including original HTML bytes. Deduplicate byte reads.
Missing, unsupported
or over-budget local images remain unavailable with a visible bounded reason
through the preview's existing warning presentation; do not silently remove
them or pretend they loaded. Pass one bounded aggregate warning to the existing
`recordError` closure from Paper's FrameAddon on DOM readiness; its private
channel, error limits and existing header warning already carry it to HtmlPreview.
No new page-to-app message or public callback is introduced. SVG image content cannot gain script or external
load authority.

Add member-only `GET /paper/files/{file_id}/preview/state` returning the captured
revision and a dependency key. Hash HTML identity/path/bytes and each normalized
image target's identity/path/hash or missing/unsupported/budget state. HtmlPreview
checks this state when its file or saved-tree revision changes. On entry, show
loading until the check completes. Retain the existing iframe when the key is
unchanged; unrelated saves therefore preserve scroll and page state. When the
key changes, load `/preview?revision=<captured revision>`, whose content URL pins
the same revision. Store that URL with the key so later unrelated revisions do
not navigate the frame. Abort obsolete state reads. Replacing, renaming or adding
a referenced image must refresh the open preview even if HTML bytes did not
change. Scale survives that refresh. Historical previews remain pinned.

Paper HTML does not take the app's theme. Make that existing visual boundary
explicit as `follow_appearance=False` on `inline_viewer_document`, passed into
its trusted inline configuration. Default remains true for ordinary inline
artifacts. In `artifact_inline.js`, Paper loads content once and does not reload
it on theme/color-mode mutation. This preserves the already loaded page after
unrelated saves prune its captured revision, without renewing its URL or adding
a new cross-frame message. Closing/reopening the live preview runs a fresh state
check at the current saved revision. An explicitly requested historical URL
keeps ordinary retention/refusal behavior. Test open HTML, prune the old saved
revision through unrelated saves, change theme, and verify no content navigation
or loss; remount must use current state. Generic inline artifacts still reload
their themed content normally.

## Zoom and commenting

Add `−`, a percentage reset button, and `+` to the existing Paper HTML panel
toolbar before Comment. The actual-component mock was rendered at 1440px and
390px using the existing app styling. It introduces no extra labels or panels.

Own scale in HtmlPreview and transform its existing `frame` iframe, which hosts
the trusted inline shell; do not access the opaque author document. The parent
clips overflow, and the iframe uses width/height `100 / scale` percent,
transform `scale(scale)` and origin `0 0`, preserving its physical viewport.
No zoom protocol is needed. Key HtmlPreview by file ID so each newly selected
file starts at 100% without one render at the previous file's scale, and
retain the chosen scale while that file stays open. Clamp the scale using a
fixed preset list `[0.5, 0.75, 1, 1.25, 1.5, 2]`, disable controls at its ends, and reset through the
percentage button. Do not reload the page to zoom or enlarge the RCP window.

Reuse the existing shell and selection messages. Verify text and box selections,
scrolling, comment-window anchors and clearing at non-100% scale; change that
geometry only if the real journey proves it necessary. Zoom clears an active
selection through useInlineShell's existing clear message and cancellation
handler, preserving the comment draft for a new selection. Expose that existing
clear action from the hook; add no message shape. Zoom is Paper HTML scope,
not a replacement universal viewer or new cross-app zoom setting.

## Artifact authoring defaults

Clarify the existing shared reply contract: ongoing record notes and requested
revisions update the original tracked file; a new file is for a distinct
deliverable or an explicit request for a separate copy. Repeating the basename
in a new turn directory does not preserve identity. Do not infer identity from
basename or automatically merge unrelated artifacts in storage.

When a viewable artifact is the content being delivered or materially revised,
show it with the existing Markdown embed syntax in the reply. Keep ordinary
prose answers as prose and avoid embedding irrelevant supporting files. Use the
same contract across Project, Space and other existing callers. Bump
`CHAT_MASTER_CONTEXT_VERSION` to 29, `WORK_POLICY_VERSION` to work-v31,
`DISCUSS_POLICY_VERSION` to discuss-v15, `EXPERIMENT_LOOP_POLICY_VERSION` to
experiment-loop-v20 and `SPACE_MASTER_POLICY` to space-master-v6; the Space key
also hashes its fixed contract. Paper's independent helper fix uses coach-v12.
These keys make existing sessions receive changed policy. Preserve
all grants, write roots, artifact publication and versioning mechanics.

## Alternatives and ownership

Reusing the existing KaTeX stack avoids a second Python TeX implementation and
dependency. A browser bundle costs more response bytes but needs no additional
server runtime. A second nested script-free Markdown frame adds layout and
selection work without reducing author authority compared with the selected
explicitly trusted renderer. Promoting transitive micromark packages and
duplicating syntax wiring solely to reduce bundle size is unnecessary.

| Action | Files / owner |
| --- | --- |
| Create | `web/src/core/markdownSyntax.ts`, `web/src/artifacts/artifactRenderer.tsx`, `web/build/artifactRenderer.ts`, `src/rcp/artifact_renderer.py`, `src/rcp/paper/html_preview.py`. |
| Modify | `web/vite.config.ts`, `web/src/core/chatMarkdown.ts` (re-export shared syntax for existing consumers). |
| Modify | `src/rcp/artifact_views.py` for Markdown bootstrap/CSP, panel iframe sandbox and explicit appearance following; `src/rcp/artifact_inline.js` to leave unthemed Paper content loaded on theme switches. |
| Modify | `src/rcp/api/paper.py`, `src/rcp/paper/live.py` for snapshot-bound read methods, state route, images and Paper math addon. |
| Modify | `src/rcp/limits.py` for preview resource budgets. |
| Modify | `web/src/paper/PaperPreview.tsx`, `web/src/paper/paper-preview.css`, `web/src/artifacts/useInlineShell.ts` for state invalidation, zoom and existing selection clearing. |
| Modify | `src/rcp/agents/artifact_contract.py`, `src/rcp/agents/prompts.py`, `src/rcp/agents/experiment_loop_prompt.py`, `src/rcp/runs/space_chat.py` for authoring defaults and policy keys. |
| Modify | `docs/specs/paper-artifacts-and-result-views.md`, `docs/specs/providers-and-containment.md`, `docs/specs/interface-and-visual-design.md`. |
| Create | `tests/test_paper_html_preview.py`, `web/tests/artifactRenderer.browser.test.mjs`. |
| Modify | `tests/test_artifact_views.py`, `tests/test_api_paper.py`, `tests/test_web_assets.py`, `tests/test_chat_prompt_protocol.py`, `tests/test_space_chat_runtime.py`, `tests/test_work_artifact_edit.py`. |
| Modify | `web/tests/chatMarkdown.test.mjs`, `web/tests/inlineArtifactShell.browser.test.mjs`, `web/tests/paperPreviewComments.browser.test.mjs`, `web/tests/paperCoach.browser.test.mjs` (existing HTML-selection fixture), `web/tests/agentThreads.browser.test.mjs`. |
| Delete | This temporary design after its rules are in owning current-behavior specs. |

## Ordered implementation and checks

1. Add the shared syntax/options module, runtime entry and build plugin. Verify
   normal/clean builds and an active Vite watch rebuild after a syntax/runtime
   edit. Test `web_dist_path()` with source, wheel-like and frozen-like layouts,
   actual packaged-resource inventory, and a missing-resource refusal. Leave
   current preview callers unchanged until these build checks pass.
2. Add failing Markdown artifact and Paper HTML math fixtures, then connect the
   shared runtime. Serve real production CSP headers in browser fixtures; the
   existing shell fixture must stop dropping them. Check math delimiters, code, malformed/unsafe TeX, raw HTML, tables, inert
   links/images, truncation and inline/panel appearance. Verify no remote loads,
   executable source injection, parent access or new RCP calls. HTML parity covers
   all four delimiters, escaped dollars, unmatched delimiters, malformed TeX,
   unsafe commands and untouched script/style/code nodes.
3. Add failing Paper asset/state API journeys, then selective transactional
   reads, state key and image inlining. Exercise existing and missing relative PNG/SVG assets,
   path escape attempts, historical revisions and resource budgets. Verify
   historical HTML uses historical image bytes. Replace/rename/add an image
   without editing HTML and observe refresh; save an unrelated file and observe
   no frame navigation. Inspect dimensions, warnings, network/console/server logs.
4. Add failing zoom/comment journeys in `paperPreviewComments.browser.test.mjs`,
   then toolbar and wrapper transform. Check in/out/reset and bounds, scroll,
   text and box selection/anchor positions below and above 100%, active-selection
   clearing and comment-draft preservation at desktop and 390px. Separately
   clarify artifact defaults and verify retained-master refresh plus existing
   in-place version and Project/Space embed workflows; never test exact prose.
5. Run affected tests, Web build, required hooks/typecheck, scoped quality and
   mutation checks, independent code review and owning-spec EGM audit. Inspect
   eventual PR discussion and inline feedback. No push, live migration or
   deployment is included in this design approval.
