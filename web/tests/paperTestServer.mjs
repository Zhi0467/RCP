import { EventEmitter, once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "vite";
import * as Y from "yjs";
import * as encoding from "lib0/encoding";
import * as decoding from "lib0/decoding";
import * as sync from "y-protocols/sync";

/** An in-process y-websocket room: update broadcast and durable ack are independent. */
export class PaperTestRoom {
  doc = new Y.Doc();
  clients = new Set();
  acknowledge = true;
  hold = false;
  pending = [];
  events = new EventEmitter();
  updatesReceived = 0;
  /** Opt-in: drop client messages unread, so local text stays unsent until a reconnect. */
  drop = false;
  /** Opt-in: a close code that refuses each new connection before any message, as admission does. */
  refuseConnect = null;
  /** Opt-in: close a connection with 4413 when one message exceeds this many bytes. */
  refuseAbove = Infinity;

  constructor(text = "draft") {
    this.doc.getText("text").insert(0, text);
    this.doc.on("update", (update) => {
      const encoder = encoding.createEncoder();
      encoding.writeVarUint(encoder, 0);
      sync.writeUpdate(encoder, update);
      for (const client of this.clients) client.send(Buffer.from(encoding.toUint8Array(encoder)));
    });
  }

  ack(recipient) {
    const encoder = encoding.createEncoder();
    encoding.writeVarUint(encoder, 100);
    encoding.writeUint8Array(encoder, Y.encodeStateVector(this.doc));
    for (const client of recipient ? [recipient] : this.clients)
      client.send(Buffer.from(encoding.toUint8Array(encoder)));
  }

  connect(client) {
    if (this.refuseConnect !== null) {
      client.close({ code: this.refuseConnect });
      return;
    }
    this.clients.add(client);
    client.onClose(() => this.clients.delete(client));
    const encoder = encoding.createEncoder();
    encoding.writeVarUint(encoder, 0);
    sync.writeSyncStep1(encoder, this.doc);
    client.send(Buffer.from(encoding.toUint8Array(encoder)));
    client.onMessage((message) => {
      if (this.drop) return;
      if (message.byteLength > this.refuseAbove) {
        client.close({ code: 4413 });
        return;
      }
      const packet = decoding.createDecoder(new Uint8Array(message));
      if (decoding.readVarUint(packet) === 0 && decoding.readVarUint(packet) === 2) {
        this.updatesReceived += 1;
        this.events.emit("update-received");
      }
      const receive = () => {
        const decoder = decoding.createDecoder(new Uint8Array(message));
        const type = decoding.readVarUint(decoder);
        if (type === 0) {
          const reply = encoding.createEncoder();
          encoding.writeVarUint(reply, 0);
          sync.readSyncMessage(decoder, reply, this.doc, client);
          if (encoding.length(reply) > 1) client.send(Buffer.from(encoding.toUint8Array(reply)));
          if (this.acknowledge) this.ack(client);
        } else if (type === 1) {
          for (const peer of this.clients) if (peer !== client) peer.send(message);
        }
      };
      if (this.hold) this.pending.push(receive);
      else receive();
    });
  }

  async waitForUpdates(count) {
    const signal = AbortSignal.timeout(10_000);
    while (this.updatesReceived < count) await once(this.events, "update-received", { signal });
  }

  /** Resolves once the room's text equals `expected`. */
  waitForText(expected, timeout = 5_000) {
    const text = this.doc.getText("text");
    return new Promise((resolve, reject) => {
      const check = () => {
        if (text.toString() !== expected) return;
        finish();
        resolve();
      };
      const timer = setTimeout(() => {
        finish();
        reject(new Error(`Room text stayed ${JSON.stringify(text.toString())}`));
      }, timeout);
      const finish = () => {
        clearTimeout(timer);
        this.doc.off("update", check);
      };
      this.doc.on("update", check);
      check();
    });
  }

  flush() {
    this.hold = false;
    for (const receive of this.pending.splice(0)) receive();
  }
}

export const paperTestTree = {
  max_doc_bytes: 4 * 1024 * 1024,
  revision: 1,
  saved_revision: 1,
  published_revision: 1,
  publish_state: "published",
  hold: null,
  files: [{ file_id: "f1", path: "intro.md", kind: "text", format: "markdown", size: 5 }],
};
export const paperTestCard = {
  change_id: "c1",
  group_id: "g1",
  task_id: "t1",
  kind: "edit",
  file_id: "f1",
  path: "intro.md",
  lines: { start: 1, end: 1 },
  preview: { removed: ["draft"], added: ["revised"], more: 0 },
  state: "pending",
};

/** The REST contract behind each test page's routes. */
export function createPaperFixture({
  room = new PaperTestRoom(),
  cards = [],
  proposedTexts = {},
} = {}) {
  const tree = structuredClone(paperTestTree);
  const posts = [];
  const rooms = new Map([["f1", room]]);
  let nextFile = 10;
  const versions = [
    {
      version_id: "v1",
      revision: 1,
      name: "First draft",
      actor_name: "Member",
      created_at: new Date().toISOString(),
      kind: "named",
      manifest: tree.files,
    },
  ];
  const oldText = room.doc.getText("text").toString();
  function replace(text) {
    const content = room.doc.getText("text");
    room.doc.transact(() => {
      content.delete(0, content.length);
      content.insert(0, text);
    });
    tree.revision += 1;
    tree.saved_revision = tree.revision;
    room.ack();
  }
  return {
    room,
    rooms,
    tree,
    cards,
    posts,
    versions,
    respond(path, method = "GET", body = {}) {
      if (method === "POST") posts.push({ path, body });
      if (path === "/api/health")
        return { json: { instance_id: "instance", data_dir_id: "data", version: "test" } };
      if (path === "/api/identity")
        return {
          json: {
            space_id: "space",
            space_kind: "personal",
            space_name: null,
            user: { user_id: "member", display_name: "Member", identity_kind: "local_owner" },
          },
        };
      if (path.endsWith("/tree")) return { json: tree };
      if (path.endsWith("/sessions")) return { json: [] };
      if (path.endsWith("/engines"))
        return {
          json: { latex: "ready", typst: "ready", errors: { latex: null, typst: null } },
        };
      if (path.endsWith("/changes"))
        return {
          json: {
            groups: cards.map((change) => ({
              base_revision: 1,
              group_id: change.group_id,
              task_id: change.task_id,
              changes: [change],
            })),
          },
        };
      const proposed = path.match(/\/changes\/([^/]+)\/proposed$/);
      if (proposed && proposed[1] in proposedTexts)
        return { body: proposedTexts[proposed[1]], contentType: "text/plain" };
      if (/\/changes\/[^/]+\/accept$/.test(path)) return { json: {} };
      const rejected = path.match(/\/changes\/([^/]+)\/reject$/);
      if (rejected) {
        for (const change of cards.filter((item) => item.group_id === rejected[1]))
          change.state = "rejected";
        return { json: {} };
      }
      if (method === "POST" && (path.endsWith("/files") || path.endsWith("/uploads"))) {
        const file_id = `f${nextFile++}`;
        const text = body.text ?? "";
        const kind = /\.(png|jpg)$/.test(body.path) ? "binary" : "text";
        tree.files.push({
          file_id,
          path: body.path,
          kind,
          format: kind === "text" ? "markdown" : "image",
          size: text.length,
        });
        rooms.set(file_id, new PaperTestRoom(text));
        tree.revision += 1;
        tree.saved_revision = tree.revision;
        return { json: { revision: tree.revision, file_ids: [file_id] } };
      }
      if (method === "POST" && /\/folders\/(rename|delete)$/.test(path)) {
        const under = tree.files.filter((file) => file.path.startsWith(`${body.path}/`));
        if (path.endsWith("/delete"))
          tree.files = tree.files.filter((file) => !under.includes(file));
        else for (const file of under) file.path = body.to + file.path.slice(body.path.length);
        tree.revision += 1;
        tree.saved_revision = tree.revision;
        return { json: { revision: tree.revision, file_ids: under.map((file) => file.file_id) } };
      }
      if (path.endsWith("/folders/archive")) return { body: "zip", contentType: "application/zip" };
      if (path.endsWith("/raw"))
        return { body: room.doc.getText("text").toString(), contentType: "text/plain" };
      if (path.endsWith("/history")) {
        if (method === "POST")
          versions.push({ ...versions[0], version_id: `v${versions.length + 1}`, name: body.name });
        return { json: versions };
      }
      if (/\/history\/[^/]+\/files\/[^/]+$/.test(path))
        return { body: oldText, contentType: "text/plain" };
      if (/\/history\/[^/]+\/restore$/.test(path)) {
        replace(oldText);
        return { json: {} };
      }
      if (/\/history\/[^/]+$/.test(path)) {
        const index = versions.findIndex((version) => path.endsWith(`/${version.version_id}`));
        if (method === "DELETE") return { json: versions.splice(index, 1)[0] };
      }
      if (path.endsWith("/hold/resolve")) {
        tree.hold = null;
        return { json: {} };
      }
      return { status: 404, json: { detail: `Unmocked fixture route: ${method} ${path}` } };
    },
  };
}

export function pdfBytes(count) {
  const objects = ["<< /Type /Catalog /Pages 2 0 R >>", ""];
  const children = [];
  for (let index = 0; index < count; index++) {
    children.push(`${objects.length + 1} 0 R`);
    objects.push("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << >> >>");
  }
  objects[1] = `<< /Type /Pages /Count ${count} /Kids [${children.join(" ")}] >>`;
  let body = "%PDF-1.4\n";
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(body));
    body += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const start = Buffer.byteLength(body);
  body += `xref\n0 ${offsets.length}\n0000000000 65535 f \n`;
  body += offsets
    .slice(1)
    .map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`)
    .join("");
  body += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`;
  return Buffer.from(body);
}

/** A Vite server for Paper pages that never proxies to a real backend. Test plugins change
 * Vite's dependency hash, so each server keeps a private cache instead of re-optimizing the
 * shared one under other test files' open pages. */
export async function createPaperViteServer(plugins = []) {
  const cacheDir = await mkdtemp(join(tmpdir(), "rcp-paper-vite-"));
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    cacheDir,
    logLevel: "error",
    // Dependencies the entry crawl misses would be found mid-test, and Vite reloads the page.
    optimizeDeps: {
      entries: ["tests/fixtures/paperResource.html"],
      include: ["@tauri-apps/api/core", "qrcode-generator"],
    },
    server: { host: "127.0.0.1", port: 0 },
    plugins: [
      {
        name: "paper-test-no-proxy",
        configResolved(config) {
          config.server.proxy = undefined;
        },
      },
      ...plugins,
    ],
  });
  await server.listen();
  return {
    server,
    origin: `http://127.0.0.1:${server.httpServer.address().port}`,
    close: async () => {
      await server.close();
      await rm(cacheDir, { recursive: true, force: true });
    },
  };
}

/** Serves the Paper page; each test routes its API and websocket in the browser. */
export const createPaperTestServer = () => createPaperViteServer();

export function editorMatches(expected) {
  const source = document.querySelector(".cm-content")?.cloneNode(true);
  source
    ?.querySelectorAll(".paper-inline-suggestion, .cm-ySelectionCaret")
    .forEach((widget) => widget.remove());
  const lines = [...(source?.querySelectorAll(".cm-line") ?? [])];
  return lines.map((line) => line.textContent).join("\n") === expected;
}

export async function openPaper(
  t,
  {
    browser,
    origin,
    room = new PaperTestRoom(),
    width = 1280,
    height = 844,
    cards = [],
    proposedTexts = {},
    initialText = "draft",
    touch = false,
    configure = () => {},
    /** A script run before the page, e.g. a desktop-runtime stub. */
    init = null,
  },
) {
  const context = await browser.newContext({
    viewport: { width, height },
    hasTouch: touch,
    isMobile: touch,
  });
  if (init) await context.addInitScript(init);
  t.after(async () => {
    await context.close();
  });
  const page = await context.newPage();
  const errors = [];
  const requests = [];
  page.on("request", (request) => requests.push(request.url()));
  const fixture = createPaperFixture({ room, cards, proposedTexts });
  configure(fixture);
  const posts = fixture.posts;
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await context.route("**/api/**", async (route) => {
    const request = route.request();
    let body;
    if (request.headers()["content-type"]?.startsWith("multipart/form-data")) {
      const form = await new Response(request.postDataBuffer(), {
        headers: { "content-type": request.headers()["content-type"] },
      }).formData();
      body = { path: form.get("path"), text: await form.get("file").text() };
    } else body = request.postDataJSON();
    return route.fulfill(fixture.respond(new URL(request.url()).pathname, request.method(), body));
  });
  await context.routeWebSocket(/\/paper\/files\/[^/]+\/live(?:\?.*)?$/, (socket) => {
    const id = new URL(socket.url()).pathname.split("/").at(-2);
    (fixture.rooms.get(id) ?? room).connect(socket);
  });
  await page.goto(`${origin}/tests/fixtures/paperResource.html`);
  await page.locator(".cm-content").waitFor();
  await page.waitForFunction(editorMatches, initialText);
  return { page, context, room, errors, posts, requests, fixture };
}

/** Types at the end of the open document. */
export async function appendText(page, text) {
  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.insertText(text);
}

/** Waits for the exact durable state, not the debounced visible one. */
export async function waitForSaved(page, value) {
  await page.waitForFunction(
    (expected) =>
      document.querySelector('[data-testid="paper-saved"]')?.getAttribute("data-saved") ===
      String(expected),
    value,
  );
}

/** A second tab of the same member: same context, so the same IndexedDB and routes. */
export async function openPaperTab(t, opened, origin) {
  const page = await opened.context.newPage();
  t.after(() => page.close());
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto(`${origin}/tests/fixtures/paperResource.html`);
  await page.locator(".cm-content").waitFor();
  return { page, errors };
}

/** A routed socket whose page-side close completes, as a real server's does. A room's own
 * close handler otherwise leaves the page socket closing, holding its provider. */
export function completingSocket(socket) {
  return {
    send: (message) => socket.send(message),
    close: (options) => socket.close(options),
    onMessage: (handler) => socket.onMessage(handler),
    onClose: (handler) =>
      socket.onClose((code, reason) => {
        handler();
        socket.close({ code, reason });
      }),
  };
}
