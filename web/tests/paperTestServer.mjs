import { EventEmitter, once } from "node:events";
import { pathToFileURL } from "node:url";
import { createServer } from "vite";
import { WebSocketServer } from "ws";
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

  constructor() {
    this.doc.getText("text").insert(0, "draft");
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
    this.clients.add(client);
    client.onClose(() => this.clients.delete(client));
    const encoder = encoding.createEncoder();
    encoding.writeVarUint(encoder, 0);
    sync.writeSyncStep1(encoder, this.doc);
    client.send(Buffer.from(encoding.toUint8Array(encoder)));
    client.onMessage((message) => {
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

  flush() {
    this.hold = false;
    for (const receive of this.pending.splice(0)) receive();
  }
}

export const paperTestTree = {
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

/** The same REST contract powers page.route tests and the interactive fixture. */
export function createPaperFixture({
  room = new PaperTestRoom(),
  cards = [],
  proposedTexts = {},
  spaceId = "space",
} = {}) {
  const tree = structuredClone(paperTestTree);
  const posts = [];
  const versions = [
    {
      version_id: "v1",
      revision: 1,
      name: "First draft",
      actor: "Member",
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
    tree,
    cards,
    posts,
    respond(path, method = "GET", body = {}) {
      if (method === "POST") posts.push({ path, body });
      if (path === "/api/health")
        return { json: { instance_id: "instance", data_dir_id: "data", version: "test" } };
      if (path === "/api/identity")
        return {
          json: {
            space_id: spaceId,
            space_kind: "personal",
            space_name: null,
            user: { user_id: "member", display_name: "Member", identity_kind: "local_owner" },
          },
        };
      if (path.endsWith("/tree")) return { json: tree };
      if (path.endsWith("/sessions")) return { json: [] };
      if (path.endsWith("/engines")) return { json: { latex: "ready", typst: "ready" } };
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
      const decision = path.match(/\/changes\/([^/]+)\/(accept|reject|undo|refresh)$/);
      if (decision) {
        for (const change of cards.filter((item) => item.group_id === decision[1])) {
          change.state = {
            accept: "accepted",
            reject: "rejected",
            undo: "undone",
            refresh: "pending",
          }[decision[2]];
          if (decision[2] === "accept" && change.kind === "edit") {
            const text = room.doc.getText("text").toString();
            replace(
              text.replace(change.preview.removed.join("\n"), change.preview.added.join("\n")),
            );
          }
        }
        return { json: {} };
      }
      if (path.endsWith("/undo-all")) {
        for (const change of cards.filter(
          (item) => item.task_id === body.task_id && item.state === "applied",
        ))
          change.state = "undone";
        return { json: {} };
      }
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
      if (/\/history\/[^/]+$/.test(path))
        return { json: versions.find((version) => path.endsWith(`/${version.version_id}`)) };
      if (path.endsWith("/hold/resolve")) {
        tree.hold = null;
        return { json: {} };
      }
      return { status: 404, json: { detail: `Unmocked fixture route: ${method} ${path}` } };
    },
  };
}

export async function createPaperTestServer({ port = 8477, fixture = null } = {}) {
  const websocket = new WebSocketServer({ noServer: true });
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    optimizeDeps: { entries: ["tests/fixtures/paperResource.html"] },
    server: { host: "127.0.0.1", port, strictPort: port !== 0 },
    plugins: [
      {
        name: "paper-test-fixture",
        configResolved(config) {
          config.server.proxy = undefined;
        },
        configureServer(vite) {
          vite.middlewares.use(async (request, response, next) => {
            const url = new URL(request.url, "http://localhost");
            if (
              url.pathname === "/tests/fixtures/paperResource.html" &&
              url.searchParams.get("phone") === "1"
            ) {
              response.setHeader("Content-Type", "text/html");
              response.end(
                '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Paper phone fixture</title></head><body style="margin:0"><iframe title="Paper at 390px" src="/tests/fixtures/paperResource.html" style="display:block;width:390px;height:844px;border:0"></iframe></body></html>',
              );
              return;
            }
            if (
              !fixture ||
              (!url.pathname.startsWith("/api/") && !url.pathname.startsWith("/__paper_test__/"))
            )
              return next();
            let body = "";
            for await (const chunk of request) body += chunk;
            const payload = body ? JSON.parse(body) : {};
            let result;
            if (url.pathname === "/__paper_test__/state")
              result = {
                json: {
                  text: fixture.room.doc.getText("text").toString(),
                  posts: fixture.posts,
                },
              };
            else if (url.pathname === "/__paper_test__/control") {
              if (typeof payload.hold === "boolean") fixture.room.hold = payload.hold;
              if (typeof payload.acknowledge === "boolean")
                fixture.room.acknowledge = payload.acknowledge;
              if (payload.flush) fixture.room.flush();
              if (payload.ack) fixture.room.ack();
              result = { json: {} };
            } else result = fixture.respond(url.pathname, request.method, payload);
            response.statusCode = result.status ?? 200;
            response.setHeader("Content-Type", result.contentType ?? "application/json");
            response.end(result.body ?? JSON.stringify(result.json));
          });
        },
      },
    ],
  });
  server.httpServer.on("upgrade", (request, socket, head) => {
    if (fixture && /\/paper\/files\/[^/]+\/live(?:\?.*)?$/.test(request.url)) {
      websocket.handleUpgrade(request, socket, head, (client) =>
        fixture.room.connect({
          send: (message) => {
            if (client.readyState === 1) client.send(message);
          },
          onClose: (callback) => client.on("close", callback),
          onMessage: (callback) => client.on("message", callback),
        }),
      );
    }
  });
  await server.listen();
  return {
    origin: `http://127.0.0.1:${server.httpServer.address().port}`,
    async close() {
      for (const client of websocket.clients) client.terminate();
      websocket.close();
      await server.close();
      fixture?.room.doc.destroy();
    },
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const fixture = createPaperFixture({
    cards: [{ ...paperTestCard }],
    spaceId: `paper-fixture-${Date.now()}`,
  });
  const server = await createPaperTestServer({ fixture });
  console.log(`${server.origin}/tests/fixtures/paperResource.html`);
  console.log(`${server.origin}/tests/fixtures/paperResource.html?phone=1`);
  const close = async () => {
    await server.close();
    process.exit(0);
  };
  process.once("SIGTERM", close);
  process.once("SIGINT", close);
}
