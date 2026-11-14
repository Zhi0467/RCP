import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
after(() => server.close());
const where = await server.ssrLoadModule("/src/comments/where.ts");
const model = await server.ssrLoadModule("/src/comments/react/threadModel.ts");
const { blankFrontMatter } = await server.ssrLoadModule("/src/paper/frontMatter.ts");
const { markdownPaperPath } = await server.ssrLoadModule("/src/paper/typstPreview.ts");
const { conversationTurnRequest } = await server.ssrLoadModule("/src/chat/chatWorkspace.ts");
const { findText } = await server.ssrLoadModule("/src/comments/anchors.ts");

const text = (where) => ({ kind: "text", text: "q", surrounding_text: "q", comment: "", where });

test("a preview text anchor says so, and every other kind but whole pins in the preview", () => {
  assert.equal(where.drawnInPreview(text(where.linesWhere("a.md", 3, 3))), false);
  assert.equal(
    where.drawnInPreview(text(where.previewWhere(where.linesWhere("a.md", 3, 5)))),
    true,
  );
  assert.equal(where.drawnInPreview({ kind: "whole", comment: "" }), false);
  assert.equal(
    where.drawnInPreview({ kind: "pdf", page: 1, rect: null, text: "", comment: "" }),
    true,
  );
  assert.equal(where.linesWhere("a.md", 3, 5), "a.md:3-5");
  const long = where.previewWhere("x".repeat(400));
  assert.ok(long.length <= 256 && where.drawnInPreview(text(long)));
});

test("pdf and box places name pages, area, and the nearby source", () => {
  assert.equal(
    where.pdfWhere(
      "main.tex",
      2,
      3,
      where.boxArea({ x: 0.1, y: 0.2, width: 0.3, height: 0.4 }),
      "intro.tex line 9",
    ),
    "main.tex, pages 2-3, area 10-40% across and 20-60% down, near intro.tex line 9",
  );
});

test("a retried thread send keeps its key until it succeeds", () => {
  let n = 0;
  const keys = model.sendKeys(() => `k${++n}`);
  assert.equal(keys.key(["b", "a"]), "k1");
  assert.equal(keys.key(["a", "b"]), "k1");
  assert.equal(keys.key(["a"]), "k2");
  keys.done();
  assert.equal(keys.key(["a"]), "k3");
});

test("only the asked targets' threads show, numbered as their pins in drawn order", () => {
  const thread = (id, target, state = "open") => ({
    thread_id: id,
    target_kind: "paper",
    target_id: target,
    state,
  });
  const shown = model.targetThreads(
    [thread("1", "f1"), thread("2", "f2"), thread("3", "f1", "resolved"), thread("4", "f1")],
    "paper",
    ["f1"],
  );
  assert.deepEqual(
    shown.map((item) => item.thread_id),
    ["1", "3", "4"],
  );
  assert.deepEqual(
    [...model.pinNumbers(shown)],
    [
      ["1", 1],
      ["4", 2],
    ],
  );
});

test("the surrounding text re-finds a repeated quote after an edit before it", () => {
  const doc = "one quote here and another quote there";
  const start = doc.lastIndexOf("quote");
  const around = model.surroundingText(doc, start, start + 5, 8);
  const edited = `prefix ${doc}`;
  assert.equal(findText(edited, "quote", around).start, edited.lastIndexOf("quote"));
});

test("front matter is blanked line for line; anything else is unchanged", () => {
  assert.equal(blankFrontMatter("---\ntitle: A\n---\n# Body\n"), "\n\n\n# Body\n");
  assert.equal(blankFrontMatter("---\r\na: 1\r\n...\r\nBody"), "\n\n\nBody");
  assert.equal(blankFrontMatter("Text\n---\nmore\n---\n"), "Text\n---\nmore\n---\n");
  assert.equal(blankFrontMatter("---\nnever closed\n"), "---\nnever closed\n");
});

test("a site-root reference names a file from the Paper root, never above it", () => {
  assert.equal(markdownPaperPath("_posts/a.md", "/images/blog/x.svg"), "images/blog/x.svg");
  assert.equal(markdownPaperPath("_posts/a.md", "/../x.svg"), null);
  assert.equal(markdownPaperPath("_posts/a.md", "https://example.invalid/x.svg"), null);
});

test("a turn carrying only comment threads is a valid request", () => {
  const request = conversationTurnRequest({
    kind: "project_chat",
    config: { provider: "codex", model: "", reasoning: "medium", run_on: "local" },
    nodeId: null,
    message: "",
    chatId: "c1",
    sessionId: null,
    commentThreadIds: ["t1"],
  });
  assert.deepEqual(request.comment_thread_ids, ["t1"]);
  assert.throws(() =>
    conversationTurnRequest({
      kind: "project_chat",
      config: { provider: "codex", model: "", reasoning: "medium", run_on: "local" },
      nodeId: null,
      message: " ",
      chatId: "c1",
      sessionId: null,
    }),
  );
});
