# Artifact page reliability

Status: design, not started. One PR. Nothing is implemented yet.

## Problems

1. **The agent's page check does not match RCP.** The artifact contract tells
   an agent with a browser to serve its page with `python3 -m http.server`
   (`agents/artifact_contract.py`). That server sends none of RCP's rules. A
   page that loads Chart.js from a CDN works in the agent's browser and comes
   up blank in RCP, whose CSP allows only inline scripts and `data:`/`blob:`
   images (`artifacts.py`, the artifact CSP).
2. **A broken page is silent.** When a page script throws, the embed or viewer
   shows whatever drew before the error, often nothing. The human cannot tell
   a broken page from an empty one.
3. **Agents rebuild charts every time.** With CDNs blocked, each agent writes
   its own chart and table code, with uneven axes, missing-data handling, and
   theming.

Context: this came from comparing RCP with ChatGPT's generated UI, which
validates model output against a component catalog and keeps the last good
render. RCP keeps arbitrary HTML in an opaque sandbox; this PR borrows only the
parts that fit that model.

## Settled decisions

- One PR for all three.
- (1) **Faithful preview, no static checker.** No collection-time HTML lint and
  no stored diagnostics: a browser check that matches RCP covers the same
  failures with real evidence. RCP ships a small self-contained preview server
  that serves the artifact directory with the same opaque-sandbox CSP and the
  same `--rcp-*` palette the inline view uses. The contract's `http.server`
  advice points at it instead.
- (2) **Error notice, keep the frame.** The artifact bootstrap records uncaught
  errors and unhandled rejections and relays a bounded summary over the
  existing private channel. The viewer and the inline embed show one line,
  "This page hit an error: <message>", under the frame; what did draw stays.
  No blank-page detection (canvases and live pages waiting for data look
  blank) and no automatic last-good render.
- (2) One click on the notice opens the existing Comment box prefilled with the
  error. The human sends it; nothing reaches the agent without that click.
- (3) **Copy-in kit, not injected.** A new official skill carries vetted
  `lineChart` and `sortableTable` functions in a reference file. The agent
  pastes them into its page, so every page stays one self-contained file that
  works the same inline, in Expand, and as a Download, and a later kit change
  never redraws an old figure.

## Plan

### Faithful preview

- One self-contained module (standard library only) owns the artifact CSP
  string and the preview server. `artifacts.py` imports the CSP from it, so the
  viewer and the preview cannot drift. The module is shipped to the run's
  execution host from its source file, local and SSH alike, like other
  remote-executed code.
- It serves the artifact directory on `127.0.0.1`, sends each HTML response with
  `Content-Security-Policy: sandbox allow-scripts; <artifact CSP>`, and prepends
  the palette from `artifact_theme` rendered at staging time. It does not run
  RCP's sanitizer; the browser's CSP enforcement is what blocks external loads.
  The contract says so.
- The contract's browser-check paragraph renders the command from the same
  object that stages the file. Turns without a browser keep today's advice.

### Error notice

- Listeners install in the artifact bootstrap before agent scripts, capturing
  the built-ins they need first. Message kind `rcp-artifact-error` with
  `message` (string, bounded in `limits.py`) and `count`. No stack objects or
  arbitrary rejection values are serialized.
- Every hop checks the source and shape; the shell drops events from a frame
  generation or version that is no longer shown, and rate-limits repeats.
  Rendered with `textContent` only.
- The inline caption and the viewer panel show the notice. "Ask to fix" opens
  Comment with the error text prefilled, on artifacts where Comment applies.
  Live pages keep refreshing; the notice clears on the next version.
- Invariant 10e holds: the page gains no new request capability, and the
  notice triggers nothing without a human click.

### Kit skill

- New official skill (for example `page-kit`) with `SKILL.md` and
  `references/kit.js`: `lineChart(el, {series, xLabel, yLabel})` and
  `sortableTable(el, {columns, rows})`. Both read `--rcp-*` tokens with
  fallbacks, draw missing values as gaps or "n/a", never as zero, and use no
  network or storage.
- The artifact contract names the skill where it already discusses HTML
  pages. Live pages may use the kit; the live-pages skill points to it.

## Checks

- Preview: a test serves a page with an external `<script src>` and a remote
  image through the shipped module and asserts the CSP header equals the
  viewer's; one served-app drive opens a CDN-dependent page in the agent
  browser path and sees the console CSP error. Run the module over SSH once.
- Error notice: browser tests for a synchronous throw, a rejected promise, a
  forged message from the page, a flood, hostile text, and a stale frame;
  Download and Keep stay available. A served-app drive: inline, Expand, the
  prefilled comment, and the next version clearing the notice.
- Kit: a browser test renders `references/kit.js` through the real sandbox with
  a gap in a series and a null cell, and sorts a column.
- Frozen candidate: the shipped preview module and the skill files are in the
  packaged inventory.
