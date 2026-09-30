---
id: live-pages
kind: skill
label: Live pages
version: 1.0.0
description: Build self-contained HTML artifacts that redraw from bounded job, node, episode, and file snapshots delivered by RCP.
dependencies:
---

# Live pages

Use a live page when the underlying result is still changing. Write ordinary
static HTML for a finished result. This skill grants no new read or write
permission. Put the page in the turn's named artifact directory and use only
sources admitted by the current contract. An episode report is never live.

Declare one `rcp-live` JSON script. Use the exact helper launch key; a path is
absolute on the artifact's execution host and inside the project's readable
roots. Node ids refer to the artifact's own graph target. RCP binds declarations
once per version. Invalid declarations leave a static page with a viewer notice.

The viewer relays `rcp-live-data` messages. Read `event.data.snapshots` in declaration
order; each entry also carries its kind and source key, id, or path. Do not fetch
RCP endpoints. Inline scripts and drawing need no external libraries. Render
unavailable reads explicitly, and retain the last successful display while a
source has an error. A truncated file is a bounded window, not the entire run.
Never turn a missing observation into a zero. Data sent to this page may leave
through frame navigation; the sandbox does not promise zero network access.

## Loss curve and its job

After `launch --key training` starts a job that appends records such as
`{"step":1,"loss":0.8}` to its metrics file, write this page with the real path:

```html
<!doctype html><title>Training loss</title>
<script type="application/json" id="rcp-live">{"version":1,"needs":[{"kind":"job","key":"training"},{"kind":"file","path":"/project/runs/training/metrics.jsonl","read":"tail","format":"jsonl"}]}</script>
<p id="state">Waiting for observations</p>
<svg viewBox="0 0 600 220" aria-label="Loss by training step"><polyline id="curve" fill="none" stroke="blue" stroke-width="2"/></svg>
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

Declare each metrics file explicitly: globs are unsupported. This example reads
small summary CSV files with `config,score` columns. CSV snapshots contain arrays,
including the header when reading the whole file. Prefer JSONL for large tails:
CSV byte tails beyond the read cap are unavailable because a clipped multiline
record cannot be decoded reliably. Add the actual job needs
when the grid should receive a saved final snapshot after those jobs end.
Node/file-only pages keep refreshing while visible and never become final.

```html
<!doctype html><title>Sweep scores</title>
<script type="application/json" id="rcp-live">{"version":1,"needs":[{"kind":"file","path":"/project/runs/sweep/a.csv","read":"whole","format":"csv"},{"kind":"file","path":"/project/runs/sweep/b.csv","read":"whole","format":"csv"}]}</script>
<table><thead><tr><th>Configuration</th><th>Score</th></tr></thead><tbody id="grid"></tbody></table>
<script>
addEventListener('message', ({data}) => {
  if (data?.kind !== 'rcp-live-data') return;
  const grid = document.getElementById('grid');
  grid.replaceChildren();
  for (const source of data.snapshots) {
    const rows = source.error ? [[source.path, source.error]] : source.rows.slice(1);
    for (const row of rows) {
      const tr = grid.insertRow();
      tr.insertCell().textContent = row[0];
      tr.insertCell().textContent = row[1];
    }
    if (source.truncated) {
      const cell = grid.insertRow().insertCell();
      cell.textContent = `${source.path}: partial data`;
    }
  }
});
</script>
```

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
