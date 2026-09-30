from __future__ import annotations

import subprocess
from pathlib import Path

from rcp.artifact_comments import _comment_panel_script


def test_comment_submission_preserves_conflicts_and_sends_fresh_session(tmp_path: Path) -> None:
    script = tmp_path / "panel.js"
    script.write_text(_comment_panel_script())
    result = subprocess.run(
        ["node", "--input-type=module", "-", str(script)],
        input=r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: "", textContent: "", hidden: false, disabled: false,
    listeners: {}, replaceChildren() {}, after() {},
    addEventListener(type, listener) { this.listeners[type] = listener; },
  });
  return elements.get(id);
}
const posts = [], messages = [], saved = new Map();
let conflict = true;
let canComment = true;
let poll;
const context = {
  config: {projectId: "project", artifactId: "artifact", selectionEnabled: false,
    maxSelections: 8, stateUrl: "/state", commentsUrl: "/comments"},
  setTimeout: callback => { poll = callback; return 1; }, clearTimeout() {},
  document: {
    addEventListener() {}, createElement: () => element(Symbol()),
    getElementById: id => ["preview", "previewImage"].includes(id) ? null : element(id),
  },
  window: {addEventListener() {}, parent: {postMessage: (...args) => messages.push(args)}},
  location: {origin: "https://rcp.test"},
  localStorage: {getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value)},
  installSelectionConfirmation: () => () => {},
  describeSelection: () => "",
  fetch: async (url, options) => {
    if (url === "/state") return {ok: true, json: async () => ({
      can_comment: canComment, fresh_session_required: true, comment_unavailable_reason: null,
    })};
    assert.equal(url, "/comments");
    assert.equal(options.method, "POST");
    assert.equal(options.credentials, "same-origin");
    posts.push(JSON.parse(options.body));
    return {ok: !conflict, json: async () => conflict ? {detail: "session occupied"} : {operation_id: "edit"}};
  },
};
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);
await new Promise(resolve => setImmediate(resolve));
assert.equal(element("add").disabled, true);
assert.equal(element("add").textContent, "Edit in a new session");
element("message").value = "Update notes";
element("message").listeners.input();
assert.equal(element("add").disabled, false);
await element("add").listeners.click();
assert.equal(element("message").value, "Update notes");
assert.equal(element("notice").textContent, "session occupied");
assert.equal(messages.length, 0);
canComment = false;
await poll();
assert.equal(element("add").disabled, true);
assert.equal(element("notice").textContent, "session occupied");
canComment = true;
await poll();
assert.equal(element("add").disabled, false);
conflict = false;
await element("add").listeners.click();
assert.equal(element("message").value, "");
assert.equal(element("add").disabled, true);
assert.deepEqual(posts, Array(2).fill({message: "Update notes", selections: [], fresh_session: true}));
assert.equal(messages.length, 1);
assert.equal(messages[0][1], "https://rcp.test");
assert.deepEqual(JSON.parse(JSON.stringify(messages[0][0])), {
  type: "rcp-artifact-edit-started", version: 1, artifact_id: "artifact", operation_id: "edit",
});
assert.equal(JSON.parse([...saved.values()][0]).message, "");
""",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
