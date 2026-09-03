import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const nodeChatSource = await readFile(
  new URL("../src/components/NodeChat.tsx", import.meta.url),
  "utf8",
);
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("assistant answer selection opens a keyboard-accessible floating comment composer", () => {
  assert.match(nodeChatSource, /className="chat-markdown chat-annotatable-answer"/);
  assert.match(nodeChatSource, /onPointerUp=/);
  assert.match(nodeChatSource, /event\.shiftKey && event\.key\.startsWith\("Arrow"\)/);
  assert.match(nodeChatSource, /createPortal\(/);
  assert.match(nodeChatSource, /aria-label="Add annotation"/);
  assert.match(nodeChatSource, /event\.key === "Escape"/);
  assert.match(nodeChatSource, /event\.metaKey \|\| event\.ctrlKey/);
  assert.match(styles, /\.chat-annotation-composer\s*\{[\s\S]*?position: fixed/);
  assert.match(
    styles,
    /@media \(max-width: 640px\)[\s\S]*?\.chat-annotation-composer\s*\{[\s\S]*?bottom:/,
  );
});

test("staged annotations can be counted, edited, and removed before the ordinary send", () => {
  assert.match(nodeChatSource, /annotations\.length} annotation/);
  assert.match(nodeChatSource, /Comment for annotation/);
  assert.match(nodeChatSource, /updateAnnotation\(annotation\.id/);
  assert.match(nodeChatSource, /Remove annotation/);

  const send = nodeChatSource.slice(
    nodeChatSource.indexOf("const send = async"),
    nodeChatSource.indexOf("const repairGraphUpdate"),
  );
  assert.match(send, /assembleChatTurn\(message, annotations\)/);
  assert.match(send, /message: text/);
  assert.doesNotMatch(send, /annotation_context|message_id|source_id|offset/);
  assert.ok(send.indexOf("await onStartTask") < send.indexOf("setAnnotations([])"));
  assert.match(send, /setMessage\(\(current\) => \(current \? current : draftMessage\)\)/);
});
