import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { EpisodeReportLink } = await server.ssrLoadModule("/src/components/EpisodeReportLink.tsx");

after(() => server.close());

const props = {
  projectId: "project one",
  episodeId: "episode/one",
  href: "/api/projects/project%20one/episodes/episode%2Fone/report/viewer",
  children: "Open report",
  onOpenError() {},
};

// Capture the event handlers during a real React render, so hooks retain their
// normal renderer contract. Effects do not execute during this server render.
function renderLink(props) {
  let link;
  function CaptureLink() {
    link = EpisodeReportLink(props);
    return link;
  }
  renderToStaticMarkup(React.createElement(CaptureLink));
  return link;
}

test("the report link cannot drag an unresolved artifact identity", () => {
  const link = renderLink(props);
  assert.equal(link.props.draggable, false);
  let prevented = false;
  link.props.onDragStart({
    preventDefault() {
      prevented = true;
    },
    dataTransfer: {
      setData() {
        assert.fail("Unresolved reports cannot publish reference links");
      },
    },
  });
  assert.equal(prevented, true);
});

test("the report link resolves the stored report before opening it", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  const errors = [];
  globalThis.fetch = async (url) => {
    requests.push(url);
    return Response.json([
      { artifact_id: "turn-artifact", supplier: "turn" },
      { artifact_id: "report-artifact", supplier: "episode_ending" },
    ]);
  };
  try {
    let prevented = false;
    const link = renderLink({ ...props, onOpenError: (error) => errors.push(error) });
    await link.props.onClick({ preventDefault: () => (prevented = true) });
    assert.equal(prevented, true);
    assert.deepEqual(requests, ["/api/projects/project%20one/episodes/episode%2Fone/artifacts"]);
    assert.deepEqual(errors, []);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("the report link surfaces a missing report or failed lookup", async () => {
  const originalFetch = globalThis.fetch;
  try {
    for (const response of [
      Response.json([]),
      Response.json({ detail: "offline" }, { status: 503 }),
    ]) {
      globalThis.fetch = async () => response;
      const errors = [];
      const link = renderLink({ ...props, onOpenError: (error) => errors.push(error) });
      await link.props.onClick({ preventDefault() {} });
      assert.equal(errors.length, 1);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});
