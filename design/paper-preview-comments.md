# Paper preview comments and HTML preview

Status: design for review. No code yet. Before merge its rules move into
`docs/specs/paper-artifacts-and-result-views.md` (Preview, Writing coach, Selection
and comments) and this file is deleted.

## Gist

- A reader can comment on the Paper **preview**, not only on Coach answers.
  - Select text, or, with a mouse or pen, drag a box on a PDF page or an image.
  - The Comment button and the comment box are the ones chat already uses.
- Each comment records where it came from, for example `paper/notes.md, line 9`
  or `main.tex, page 3, near sections/results.tex line 118`.
- A comment waits in the Coach composer with the reader's other comments. The
  Coach tab shows how many are waiting. They go to the coach with the next message.
- `.html` Paper files get a live preview inside the same opaque sandbox that agent
  HTML artifacts use. Its text and areas can be commented on the same way.

The user settled the flow on 2026-10-29 from rendered mockups: comments wait for
the next Coach message, and PDFs get a text layer plus box drag.

## Why not make each preview an artifact

Artifacts are stored, versioned deliverables with pins, undo and their own chat.
A Paper preview changes on every save, and Paper keeps its own history. The
artifact frame would also lose synctex, links between Paper files, images from
the Paper tree, and Typst's in-place redraw. Only the comment mechanism and,
for `.html` files, the sandbox are reused.

## Terms

| Term | Meaning |
| --- | --- |
| Selection source | A part of the page whose selections can be commented on: the Coach transcript, or the preview. |
| Where | A short label naming the source: file, then line or page. Shown under the quote and sent with it. |
| Box | A rectangle dragged on a PDF page or an image, kept as fractions of the page. |

## 1. Who owns what

`useAnswerComments` moves from `PaperCoach` up to `PaperWorkspace`, which then
owns the state it reads:

| State | Owner after | Why |
| --- | --- | --- |
| `useAnswerComments` result (`comments`) | `PaperWorkspace` | The preview and the Coach both stage into it. |
| `submitting` (`busy`) | `PaperWorkspace` | A comment staged mid-send must not be cleared by the send. |
| `error` / `setError` (`onError`) | `PaperWorkspace` | Preview errors must show while Coach is hidden. |
| transcript ref, message textarea ref | `PaperWorkspace` | Inputs of the hook. |
| message draft, attachments, send | `PaperCoach` (unchanged) | Comments never touch them. |

- `PaperCoach` receives `comments`, `submitting`, `setSubmitting`, `error`,
  `setError` and the two refs as props.
- On send, `PaperCoach` clears only the comments it sent
  (`comments.clear(sent)`), so one staged during the send survives.
- The Comment button and the comment box are portalled and fixed, so they show
  over the preview while the Coach pane is hidden.

**Coach segment button.** It shows the staged count, hidden at zero, on the
desktop segment and the phone view switch. When `error` is set, the button takes
the failure style and its hover is the error text. Hover otherwise: "Comments
waiting for Coach".

## 2. Selection sources

`useAnswerComments` takes a list of selection sources instead of one transcript:

```ts
interface SelectionSource {
  root: RefObject<HTMLElement | null>;
  /** The commentable part of the selection, its quote, and where it came from; null if none. */
  resolve(selection: Selection, root: HTMLElement): { range: Range; quote: string; where?: string } | null;
}
```

- The transcript source keeps today's `.chat-annotatable-answer` clamp
  (`annotatableAnswerSelectionRange`). Every other chat passes only that source,
  so its behavior does not change.
- `useSelectionOffer` asks each source in order and offers on the first match.
  The offer carries `quote` and `where` into the composer.
- The composer state (`SelectedAnswerComment`) gains `where?`, and an `origin`
  element. `stage()` copies `where`; `stage()` and `dismiss()` return focus to
  `origin` when set, else the message field.
- A box (section 4) and an HTML frame selection (section 5) open the composer
  through `openComposer` directly, as `NodeChat` does for inline artifacts.

## 3. The staged comment

`StagedChatAnnotation` (`web/src/chat/chatInput.ts`) gains `where?: string`, at
most 200 characters.

- `parseStagedChatAnnotations` keeps it, and the `Pick` in `assembleChatTurn`
  includes it.
- The staged list shows `where` in the existing `<small>` slot under the quote.
- `assembleChatTurn` writes a comment with `where` as
  `"${where}:\n${selectedText}\ncomment: ${comment}"`. Answer comments are
  unchanged.
- Preview comments never set `artifact`: coach turns carry no
  `artifact_context`, and an `artifact` selection would be dropped from the text.
- Limits stay: 50 comments a turn, 2048 comment characters, 4096 quoted
  characters. A text selection over 4096 is refused, as today. A box quote is
  cut to 4096 with "…".

## 4. Selection and where, per preview type

| Preview | Select | Where |
| --- | --- | --- |
| Markdown | text | `path, line N` or `path, lines N-M` |
| Plain text (also `.svg`, unknown text) | text in the `<pre>` | same |
| LaTeX (compiled PDF) | text layer, or box | `root, page P, near file line N` |
| Typst (compiled PDF) | text layer, or box | `root, page P` |
| Uploaded PDF | text layer, or box | `path, page P` |
| Raster image | box | `path` |
| `.html` | text or box, inside the frame | `path` |

`root` is the compile root the preview shows, not the open file. A selection that
spans pages says `pages P-Q`, and its line comes from its last page.

**Markdown.** A rehype plugin, placed before `rehype-katex`, copies each element's
`position.start.line` and `position.end.line` to `data-line-start` and
`data-line-end`. A selection resolves to the nearest ancestor with them; display
math falls back to its block. The preview may trail typing by the
`usePreviewText` delay, so lines may lag the editor briefly.

**Plain text.** Count newlines in the `<pre>` text before the range start and end.

**PDF text layer.** `PaperPdf` adds pdf.js's `TextLayer` (`pdfjs-dist` 6.4.299)
over each page in the same `replaceChildren` call as the canvas, at the CSS-scale
viewport with `--scale-factor` set, and loads pdf.js's `.textLayer` CSS.
`.paper-pdf-page` becomes `position: relative`. The layer is cleared with the
canvas when a page leaves the window. Selection is native, so it works on touch.

**Box.** A new `PaperBoxSelect` component wraps a page slot or the image.
- Mouse and pen only. On touch the page scrolls as today; there is no box.
- A drag that starts on text-layer text is left to native selection. A drag
  that starts elsewhere draws the box.
- On release it reports the page, the box as fractions of the page, and the
  text-layer text inside it (or "Selected area").
- `PaperPdf` and the image preview take an `onBox` prop. No pixels are cropped
  or attached; the coach can open the file.

**Reverse synctex (LaTeX).** `synctex.ts` gains `synctexSource(map, page, y)`.
`y` is the selection's bottom, converted to points as the inverse of the page
scale `PaperPdf` already uses for scrolling. It searches every path in the map
for that page's marks and takes the one with the largest `y` at or below the
point. The label says "near", because `synctex.py` keeps only the first mark per
line. No mark on the page: the label is the page alone.

**Stale preview.** A comment's quote and where are fixed when it is staged. If
the PDF re-renders or the file changes while the composer is open, the composer
stays as it is.

## 5. HTML preview

**Format.** `file_format` maps `.html` and `.htm` to `"html"` only when the file
is text kind. A binary `.html` stays `"other"` and downloads.

**Routes** (member access as the other Paper routes):

- `GET /api/projects/{project_id}/paper/files/{file_id}/preview` returns the
  inline shell with `selectable: true` and no live URL. It accepts the shell's
  `theme` and `color_mode` query parameters.
- `GET …/files/{file_id}/preview/content?revision=N` returns
  `artifact_content(path, "text/html", data, frame_addon=selection_frame_addon(), inline=...)`
  with its CSP, `nosniff` and `no-store`, themed as `stored_artifact_content`
  does. `revision` reads that saved revision through `raw_file`.
- Unknown file or revision: 404. Bytes that no longer classify as HTML: 404.

**Shell.** `artifact_views` gains a public `inline_viewer_document(name, kind, *,
content_url, live, selectable)`, where `live` is `(url, artifact_id) | None`. The
artifact routes call it with their descriptor's name. The shell's error messages
show in the preview as the existing preview failure.

**Frame.** `PaperPreview` shows an `<iframe>` of the shell keyed by the saved
revision, so a save reloads it. It shows saved text, not unsaved typing.

- Comment mode is a toggle in the preview's panel tools (hover "Comment on the
  page"). When on, the page's own clicks are captured for selection, as in chat.
- The message handling in `InlineArtifact.tsx` (read the shell message, place the
  composer from the frame rect, post `rcp-inline-comment-mode` on each load, post
  `rcp-inline-selection-clear` on close) moves into a hook that `InlineArtifact`
  and `PaperPreview` both use.
- A text selection stages `selection.text`; a box stages the shell's element
  labels as its quote. Neither sets `artifact`.

**Invariant 10e** holds: Paper HTML runs one frame inside the same opaque sandbox
and CSP as agent artifacts, with no popups, forms or downloads, and never as a
same-origin document. Relative images and links to other Paper files do not load;
the page must be self-contained, as an artifact must.

## 6. What does not change

- The Coach send route and payload. Comments are text.
- The artifact viewer and its own comment panel.
- Paper history, change cards and coach suggestions.

## 7. Verification

- Web unit tests:
  - `assembleChatTurn` and `parseStagedChatAnnotations` with `where`;
  - a preview selection source returns quote and where for Markdown and plain text;
  - `synctexSource` on a small map, including a page with no marks;
  - send clears only the sent comments.
- Python: `.html` text classifies as `html`, binary as `other`; the content route
  returns the artifact CSP; unknown revision 404; non-member 404.
- Served-app journey on a throwaway data dir: comment on Markdown, on a PDF with
  text and a box, and on an HTML file with comment mode; see the count and an
  error on the Coach button; send; read the turn text. Check 390px, including
  text selection on touch.

## Rejected alternatives

- **Each preview as an artifact.** See "Why not" above.
- **Widen the transcript root to the whole page.** It would offer Comment in the
  editor and file tree, and the answer clamp does not fit preview DOM.
- **Attach a crop of each box.** Attachments cap at 8 and have no link back to a
  comment. The coach can open the file at the page and box instead.
- **Send a preview comment at once.** The user chose staging, which matches chat
  and artifact comments.
