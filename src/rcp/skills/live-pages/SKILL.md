---
id: live-pages
kind: skill
label: Live pages
version: 1.5.0
description: Build self-contained HTML artifacts that redraw from bounded graph, job, node, episode, file, and folder snapshots delivered by RCP.
dependencies:
---

# Live pages

Use a live page when the underlying result is still changing. Write ordinary
static HTML for a finished result. This skill grants no new read or write
permission. Put the page in the turn's named artifact directory and use only
sources admitted by the current contract. An episode report is never live.

Declare one `rcp-live` JSON script. Use the exact helper launch key; a path is
absolute on the artifact's execution host and inside a registered project
repository listed in the prompt or this artifact's own ready conversation/episode
worktree. RCP run stages (conversation workspace, turn folders, artifact folders)
are not readable. Node ids refer to the artifact's own graph target. RCP binds
declarations once per version. Invalid declarations leave a static page with a viewer notice.

The viewer relays `rcp-live-data` messages. Read `event.data.snapshots` in declaration
order; each entry also carries its kind and source key, id, path, or dir. Do not fetch
RCP endpoints. Inline scripts and drawing need no external libraries. Render
unavailable reads explicitly, and retain the last successful display while a
source has an error. A truncated file is a bounded window, not the entire run.
Never turn a missing observation into a zero. Data sent to this page may leave
through frame navigation; the sandbox does not promise zero network access.

## Growing sources

For sources that keep growing during a run, append records and read the tail,
or maintain a small downsampled summary. Never have the page read an
ever-growing file whole. Keep each read within the limits listed in the
artifact contract. Label a recent window or downsampled view so the reader
knows what it shows.

## Inside the reply

Embed the page where the reader needs it, with Markdown image syntax and its
absolute artifact path, so it keeps updating inside the reply itself. There the
page is painted with the reply's theme: draw with the `--rcp-*` custom
properties the prompt lists, each with a fallback for Expand and downloads,
leave the background transparent, and size from content and width rather than
the viewport. For the first example below, the reply would carry
`![Training loss](<artifact directory>/training-loss.html)` where the reader
needs the curve. The examples stay minimal; a real dashboard labels its axes,
shows the latest values as text, and says plainly when a source is unavailable.

## Loss curve and its job

After `launch --key training` starts a job that appends records such as
`{"step":1,"loss":0.8}` to its metrics file, write this page with the real path:

```html
<!doctype html><title>Training loss</title>
<script type="application/json" id="rcp-live">{"version":1,"needs":[{"kind":"job","key":"training"},{"kind":"file","path":"/project/runs/training/metrics.jsonl","read":"tail","format":"jsonl"}]}</script>
<p id="state">Waiting for observations</p>
<svg viewBox="0 0 600 220" aria-label="Loss by training step"><polyline id="curve" style="fill:none;stroke:var(--rcp-accent, #2563eb);stroke-width:2"/></svg>
<script>
addEventListener('message', ({data}) => {
  if (data?.kind !== 'rcp-live-data') return;
  const [job, metrics] = data.snapshots;
  document.getElementById('state').textContent = metrics.error || job.error ||
    `${job.state}; ${data.final ? 'saved final data' : 'updating'}${metrics.truncated ? '; recent window' : ''}`;
  if (metrics.error) return;
  const rows = metrics.rows.filter(r => Number.isFinite(r.step) && Number.isFinite(r.loss));
  if (!rows.length) return;
  const lo = Math.min(...rows.map(r => r.step));
  const span = Math.max(1, Math.max(...rows.map(r => r.step)) - lo);
  const peak = Math.max(0.001, ...rows.map(r => r.loss));
  document.getElementById('curve').setAttribute('points', rows.map(r =>
    `${10 + 580 * (r.step - lo) / span},${210 - 200 * r.loss / peak}`).join(' '));
});
</script>
```

## Sweep grid

Use one folder source for an 8-arm by 10-task grid. Create the run folder before
viewing the page; later batches may create their task files after the page binds.
This example displays each status JSON as text, so it also handles pretty-printed
JSON. Replace the root with an admitted repository or ready worktree path.
Patterns match one directory segment at a time; recursive `**` is unsupported.
Add actual job needs if the grid should save a final snapshot when those jobs
end. Pages watching only nodes, files, and folders keep refreshing while visible.

```html
<!doctype html><title>Evaluation grid</title>
<script type="application/json" id="rcp-live">{"version":1,"needs":[{"kind":"files","dir":"/project/runs/evaluation","pattern":"*/task-*/status.json","read":"whole","format":"text"}]}</script>
<p id="state">Waiting for task files</p>
<table><thead><tr><th>Arm / task</th><th>Status</th></tr></thead><tbody id="grid"></tbody></table>
<script>
addEventListener('message', ({data}) => {
  if (data?.kind !== 'rcp-live-data') return;
  const source = data.snapshots[0];
  document.getElementById('state').textContent = source.error ||
    `${source.files.length} task files${source.truncated ? '; partial data' : ''}`;
  if (source.error && !source.files.length) return;
  const grid = document.getElementById('grid');
  grid.replaceChildren();
  for (const file of source.files) {
    const row = grid.insertRow();
    row.insertCell().textContent = file.path;
    row.insertCell().textContent = file.error ||
      `${file.rows.join('\n')}${file.truncated ? ' (partial)' : ''}`;
  }
});
</script>
```

An empty folder means no observations yet. A folder or per-file error means
unavailable, never zero. JSONL snapshots decode each line; CSV snapshots contain
arrays, including the header for whole reads. Prefer JSONL for large tails:
CSV byte tails beyond the read cap are unavailable because clipped multiline
records cannot be decoded reliably.

## Live graph board

Declare exactly one parameter-free `graph` need to receive the artifact's bound
main or branch graph. It may accompany node/file/files needs, never job/episode.
Graph pages never finalize. Each node has `id`, `type`, `title`,
`title_truncated`, `status`, and `standing`; each edge has `source`, `relation`,
and `target`. Titles are bounded; ids remain whole. No bodies are sent.
Filter these arrays in your own JavaScript. The viewer caches complete graphs
and reuses them when unchanged. An over-cap read supplies an error code without
data; retain the last complete display until polling recovers. The wrapper marks
stale data with a persistent red edge.

```html
<!doctype html><title>Graph board</title>
<script type="application/json" id="rcp-live">{"version":1,"needs":[{"kind":"graph"}]}</script>
<div id="board"></div>
<script>
addEventListener('message', ({data}) => {
  if (data?.kind !== 'rcp-live-data') return;
  const graph = data.snapshots.find(s => s.kind === 'graph');
  if (!graph || graph.error || !graph.nodes) return;
  const board = document.getElementById('board');
  board.replaceChildren();
  for (const node of graph.nodes) {
    const button = document.createElement('button');
    button.textContent = node.title;
    button.onclick = () => parent.postMessage({type:'rcp-open-node', version:1, id:node.id}, '*');
    board.append(button);
  }
});
</script>
```

A node click opens RCP's native node panel only when this artifact is the active
pinned page in Artifacts. Other viewers ignore it. This one-way action returns no
acknowledgement and grants no request capability.

## Episode progress

Only episode artifact contracts offer the `episode` kind. It always means the
artifact's own episode, never an arbitrary episode id. Progress counts turns;
it is not a claim about scientific progress or success.

```html
<!doctype html><title>Episode turn progress</title>
<script type="application/json" id="rcp-live">{"version":1,"needs":[{"kind":"episode"}]}</script>
<progress id="turns"></progress><p id="status">Waiting for episode data</p>
<script>
addEventListener('message', ({data}) => {
  if (data?.kind !== 'rcp-live-data') return;
  const episode = data.snapshots[0];
  const status = document.getElementById('status');
  if (episode.error) { status.textContent = episode.error; return; }
  const progress = document.getElementById('turns');
  if (episode.turn_limit > 0 && episode.turn != null) {
    progress.max = episode.turn_limit;
    progress.value = episode.turn;
  } else progress.removeAttribute('value');
  status.textContent = `${episode.state}: turn ${episode.turn} of ${episode.turn_limit}; budget ${episode.budget_used} of ${episode.budget_limit}`;
});
</script>
```
