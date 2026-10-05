import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("Artifacts lists durable entries, refreshes and retries failures", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 626, height: 850 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let entries = [];
    let failure = false;
    await page.route("**/api/projects/project/artifacts", (route) =>
      failure
        ? route.fulfill({ status: 503, json: { detail: "Storage unavailable" } })
        : route.fulfill({ json: entries }),
    );
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/artifacts.html`,
    );
    await page.locator(".artifacts-view").waitFor();
    const entry = (artifact_id, name, fields) => ({
      id: artifact_id,
      artifact_id,
      name,
      kind: "artifact",
      view: "html",
      created_at: "2026-09-10T12:00:00Z",
      path: null,
      operation_id: null,
      episode_id: null,
      episode_mode: null,
      source_chat_href: null,
      can_open: true,
      available: true,
      can_download: false,
      download_url: null,
      unavailable_reason: null,
      viewer_url: `/viewer-shell/${artifact_id}`,
      ...fields,
    });
    entries = [
      entry("report-old", "Validation report", {
        kind: "report",
        episode_id: "old-episode",
        episode_mode: "experiment_loop",
        source_chat_href:
          "#/projects/project?view=runs&experiment=experiment%2Ftransfer&episode=old-episode&target=branch&branch=branch-one&parent=parent-episode",
      }),
      entry("plot", "Saved plot", {
        operation_id: "task",
        episode_mode: "experiment_loop",
        path: "artifacts/plot.html",
        source_chat_href: "#/projects/project?view=chats&branch_id=branch-one&chat=plot-chat",
        source_node_id: "exp/transfer",
        can_download: true,
        download_url: "/api/projects/project/tasks/task/artifacts/plot/download",
      }),
      entry("unavailable", "Unavailable plot", {
        source_node_id: "exp/not-in-this-graph",
        can_open: false,
        available: false,
        viewer_url: null,
        unavailable_reason: "Preview unavailable.",
      }),
      entry("report-no-chat", "Report", {
        kind: "report",
        episode_id: "no-chat",
        episode_mode: "auto_research",
      }),
    ];
    entries.push(
      ...["file", "pdf"].map((view) => ({
        id: `artifact:${view}`,
        name: `${view}.dat`,
        kind: "artifact",
        view,
        operation_id: "task",
        artifact_id: view,
        available: true,
        can_open: false,
        can_download: true,
        viewer_url: null,
        download_url: `/api/projects/project/tasks/task/artifacts/${view}/download`,
      })),
    );
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.getByRole("link", { name: "Open Validation report" }).waitFor();
    await page.getByRole("link", { name: "Open originating chat for Validation report" }).waitFor();

    assert.equal(await page.locator(".artifacts-list time").count(), 0);
    assert.equal(
      await page
        .getByRole("link", { name: "Open originating chat for Report", exact: true })
        .count(),
      0,
    );
    await page.getByRole("link", { name: "Open Report", exact: true }).waitFor();
    assert.equal(await page.locator(".artifact-episode-tag").count(), 3);
    assert.equal(
      await page
        .locator(".artifact-entry")
        .filter({ hasText: "Unavailable plot" })
        .locator(".artifact-episode-tag")
        .count(),
      0,
    );
    const layout = await page.evaluate(() => {
      const panel = document.querySelector(".artifacts-view");
      const heading = panel.querySelector("h2");
      const row = panel.querySelector("h3");
      return {
        paddingLeft: parseFloat(getComputedStyle(panel).paddingLeft),
        paddingRight: parseFloat(getComputedStyle(panel).paddingRight),
        headingSize: parseFloat(getComputedStyle(heading).fontSize),
        rowSize: parseFloat(getComputedStyle(row).fontSize),
        overflow: document.documentElement.scrollWidth > window.innerWidth,
      };
    });
    assert.ok(layout.paddingLeft > 0 && layout.paddingRight > 0);
    assert.ok(layout.headingSize > layout.rowSize);
    assert.equal(layout.overflow, false);

    assert.equal(
      await page.getByRole("link", { name: "Open Saved plot" }).getAttribute("href"),
      entries[1].viewer_url,
    );
    assert.equal(
      await page.getByRole("link", { name: "Open Validation report" }).getAttribute("target"),
      "_blank",
    );
    // The title and whitespace belong to the existing link, in both Aqua modes.
    const report = page.locator(".artifact-entry").filter({ hasText: "Validation report" });
    const unavailable = page.locator(".artifact-entry").filter({ hasText: "Unavailable plot" });
    const classicShadow = await report.evaluate((row) => getComputedStyle(row).boxShadow);
    for (const mode of ["light", "dark"]) {
      await page.evaluate((mode) => {
        document.documentElement.dataset.theme = "aqua";
        document.documentElement.dataset.colorMode = mode;
      }, mode);
      const raised = await report.evaluate((row) => getComputedStyle(row).boxShadow);
      assert.notEqual(raised, "none");
      assert.equal(await unavailable.evaluate((row) => getComputedStyle(row).boxShadow), "none");
      assert.equal(await unavailable.getByRole("link").count(), 0);
      await report.hover({ position: { x: 25, y: 25 } });
      await page.mouse.down();
      const pressed = await report.evaluate((row) => getComputedStyle(row).boxShadow);
      assert.match(pressed, /inset/);
      assert.notEqual(pressed, raised);
      // Moving out cancels navigation while still exercising the pressed state.
      await page.mouse.move(0, 0);
      await page.mouse.up();
      await page.setViewportSize({ width: 360, height: 780 });
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
      await page.setViewportSize({ width: 626, height: 850 });
    }
    await page.evaluate(() => {
      document.documentElement.dataset.theme = "classic";
      document.documentElement.dataset.colorMode = "light";
    });
    assert.equal(await report.evaluate((row) => getComputedStyle(row).boxShadow), classicShadow);
    failure = true;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await page.getByRole("alert").waitFor();
    failure = false;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await page.getByRole("alert").waitFor({ state: "detached" });
    await page.getByRole("link", { name: "Open Saved plot" }).waitFor();
    await page.evaluate(() => {
      window.previewCalls = [];
      window.__TAURI_INTERNALS__ = {
        invoke: async (command, args) => {
          window.previewCalls.push({ command, args });
          return { opened: true };
        },
      };
    });
    await page.route("**/api/projects/project/artifacts/*/state", (route) => {
      const artifactId = route.request().url().split("/").at(-2);
      return route.fulfill({ json: viewerState(artifactId) });
    });
    await page.route("**/viewer-shell/*", (route) =>
      route.fulfill({ contentType: "text/html", body: "<p>Artifact content</p>" }),
    );
    for (const name of ["Validation report", "Saved plot"]) {
      await page.getByRole("link", { name: `Open ${name}`, exact: true }).click();
      await page.locator(".artifact-viewer iframe").waitFor();
      assert.equal(await page.locator(".artifact-viewer").count(), 1);
      assert.equal(page.context().pages().length, 1);
      await page.getByRole("button", { name: "Close viewer", exact: true }).click();
    }
    assert.deepEqual(await page.evaluate(() => window.previewCalls), []);
    // The source link stays above the stretched preview hit target and follows
    // the same app window, even in the native shell.
    for (const theme of ["classic", "aqua"]) {
      await page.evaluate((theme) => {
        document.documentElement.dataset.theme = theme;
      }, theme);
      for (const entry of entries.slice(0, 2)) {
        const source = page.getByRole("link", {
          name: `Open originating chat for ${entry.name}`,
          exact: true,
        });
        await source.click();
        assert.equal(new URL(page.url()).hash, entry.source_chat_href);
        assert.equal(
          await page.evaluate(() => window.previewCalls.length),
          0,
          "Source chat never opens an artifact preview",
        );
      }
      const source = page.getByRole("link", {
        name: "Open originating chat for Saved plot",
        exact: true,
      });
      await source.focus();
      await page.keyboard.press("Enter");
      assert.equal(new URL(page.url()).hash, entries[1].source_chat_href);
    }
    for (const view of ["file", "pdf"]) {
      const row = page
        .locator(".artifact-entry")
        .filter({ has: page.locator(`a[download="${view}.dat"]`) });
      assert.equal(await row.locator("p").count(), 0);
      assert.equal(await row.locator("a[download]").count(), 1);
    }
    // A refresh reprojects the existing rows after entering the desktop runtime.
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.getByRole("button", { name: "Open pdf.dat", exact: true }).click();
    await page.waitForFunction(() => window.previewCalls.length === 1);
    assert.deepEqual(await page.evaluate(() => window.previewCalls[0]), {
      command: "open_artifact_pdf",
      args: { projectId: "project", artifactId: "pdf" },
    });
    // Every action sits at the row's right end, whatever the title length.
    const plot = page.locator(".artifact-entry").filter({ hasText: "Saved plot" });
    const [download, openBox, rowBox] = await Promise.all([
      plot.getByLabel("Download Saved plot", { exact: true }).boundingBox(),
      plot.getByRole("link", { name: "Open Saved plot", exact: true }).boundingBox(),
      plot.boundingBox(),
    ]);
    assert.ok(openBox.x - (download.x + download.width) < 24);
    assert.ok(rowBox.x + rowBox.width - (openBox.x + openBox.width) < 24);
    // The source node opens in place, above the stretched preview target.
    const previews = await page.evaluate(() => window.previewCalls.length);
    await page.getByRole("button", { name: "Open source node for Saved plot" }).click();
    assert.deepEqual(await page.evaluate(() => window.openedNodes), ["exp/transfer"]);
    assert.equal(await page.evaluate(() => window.previewCalls.length), previews);
    assert.equal(
      await page.getByRole("button", { name: "Open source node for Unavailable plot" }).count(),
      0,
    );
    // Coming back shows the last list at once while the refresh is in flight.
    let releaseRefresh;
    await page.route("**/api/projects/project/artifacts", async (route) => {
      await new Promise((resolve) => (releaseRefresh = resolve));
      await route.fulfill({ json: entries });
    });
    await page.evaluate(async () => {
      const { remountArtifacts } = await import("/tests/fixtures/artifacts.tsx");
      remountArtifacts();
    });
    await page.getByRole("link", { name: "Open Saved plot" }).waitFor();
    assert.equal(await page.getByRole("status").count(), 0);
    releaseRefresh?.();
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("universal cards preserve actions, refresh Keep, and resolve file citations", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/**", (route) => route.fulfill({ json: [] }));
    await page.route("**/artifacts/image/content?*", (route) => route.fulfill({ status: 410 }));
    const mutations = [];
    await page.route("**/artifacts/file/keep", (route) => {
      mutations.push({
        method: route.request().method(),
        body: route.request().postDataJSON(),
        contentType: route.request().headers()["content-type"],
      });
      return route.fulfill({ json: { kept: true } });
    });
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/artifacts.html`,
    );
    await page.evaluate(async () => {
      const { default: React } = await import("/node_modules/.vite/deps/react.js");
      const {
        default: { createRoot },
      } = await import("/node_modules/.vite/deps/react-dom_client.js");
      const { NodeChat } = await import("/src/chat/NodeChat.tsx");
      const { ArtifactViewer } = await import("/src/artifacts/ArtifactViewer.tsx");
      const profile = {
        provider: "codex",
        model: null,
        reasoning: null,
        run_on: "local",
        permissions: {},
      };
      const artifacts = ["file", "pdf", "text", "image", "html"].map((view) => ({
        artifact_id: view,
        name: `${view}.dat`,
        view,
        media_type: {
          file: "application/octet-stream",
          pdf: "application/pdf",
          text: "text/plain",
          image: "image/png",
          html: "text/html",
        }[view],
        size_bytes: 64,
        available: true,
        unavailable_reason: null,
        can_open: !["file", "pdf"].includes(view),
        can_download: true,
        can_keep: true,
        can_discuss: ["html", "image"].includes(view),
      }));
      const task = {
        operation_id: "turn",
        kind: "project_chat",
        status: "completed",
        settled: true,
        created_at: "2026-09-27T10:00:00Z",
        updated_at: "2026-09-27T10:00:00Z",
        request: { chat_id: "chat", mode: "discuss" },
        result: {
          messages: ["[file](/scratch/turns/turn/artifacts/file.dat)"],
          artifacts,
          artifact_omissions: { empty: 2, count_limit: 3, discovery_failed: false },
        },
      };
      const omitted = {
        ...task,
        operation_id: "omitted",
        result: { artifact_omissions: { discovery_failed: true } },
      };
      const element = document.createElement("div");
      document.body.replaceChildren(element);
      window.cardRoot = createRoot(element);
      window.renderCards = (desktop = false) => {
        if (desktop)
          window.__TAURI_INTERNALS__ = {
            transformCallback: () => 1,
            invoke: async (command, args) => {
              if (command === "open_artifact_pdf") window.pdfCall = { command, args };
              return { opened: true };
            },
          };
        window.cardRoot.render(
          React.createElement(
            React.Fragment,
            null,
            React.createElement(NodeChat, {
              key: String(desktop),
              project: {
                id: "project",
                name: "Project",
                repositories: [],
                project_truth_scope: [],
                machines: [{ alias: "local" }],
                agent_profiles: { project_chat: profile },
                provider_readiness: {},
              },
              node: null,
              runScope: [],
              tasks: [task, omitted],
              activeTask: null,
              historyMessages: [],
              chatId: "chat",
              onRefreshTask: async () => {
                window.refreshCount = (window.refreshCount ?? 0) + 1;
                task.result.artifacts = task.result.artifacts.map((artifact) =>
                  artifact.artifact_id === "file"
                    ? { ...artifact, can_keep: false, kept_filename: "file.dat" }
                    : artifact,
                );
                window.renderCards();
                return task;
              },
              onClose() {},
              onStartTask() {},
              onInspectTask() {},
              onOpenInbox() {},
              onRepairGraphUpdate() {},
            }),
            React.createElement(ArtifactViewer),
          ),
        );
      };
      window.renderCards();
    });
    const file = page.locator("#artifact-turn-file");
    await file.waitFor();
    assert.equal(await page.locator(".chat-artifact").count(), 5);
    assert.equal(await file.getByRole("button").count(), 1);
    assert.equal(await file.locator("a[download]").count(), 1);
    assert.equal(await page.locator("#artifact-turn-pdf button").count(), 1);
    await page.waitForFunction(() => !document.querySelector("#artifact-turn-image img"));
    assert.equal(await page.locator("#artifact-turn-image a[download]").count(), 1);
    assert.equal(await page.locator("#artifact-turn-image [data-artifact-action=keep]").count(), 1);
    assert.equal(await page.locator(".chat-artifact-omissions[role=status]").count(), 2);
    await page.locator('a[href="/scratch/turns/turn/artifacts/file.dat"]').click();
    assert.equal(await page.evaluate(() => document.activeElement.id), "artifact-turn-file");
    await file.locator("[data-artifact-action=keep]").click();
    await file.locator("[data-artifact-action=keep]").waitFor({ state: "detached" });
    assert.deepEqual(mutations, [{ method: "POST", body: {}, contentType: "application/json" }]);
    await page.evaluate(() => window.renderCards(true));
    await page.locator("#artifact-turn-pdf .chat-artifact-actions button").first().click();
    await page.waitForFunction(() => window.pdfCall);
    assert.deepEqual(await page.evaluate(() => window.pdfCall), {
      command: "open_artifact_pdf",
      args: { projectId: "project", taskId: "turn", artifactId: "pdf" },
    });
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

function viewerState(artifactId, version = 1) {
  return {
    artifact_id: artifactId,
    name: "Saved plot",
    media_type: "text/html",
    view: "html",
    supplier: "turn",
    current_version: `version-${version}`,
    version_number: version,
    can_undo: version > 1,
    live: null,
    editing_operation_id: null,
    can_comment: true,
    comment_unavailable_reason: null,
    fresh_session_required: false,
    thread_href: "#/projects/project?view=chats&chat=plot-chat",
    viewer_url: `/viewer-shell/${artifactId}?version=${version}`,
    download_url: `/api/projects/project/artifacts/${artifactId}/download`,
    can_keep: true,
    expires_at: null,
  };
}

test("viewer persists placement and follows an edit through publication and Undo", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1200, height: 850 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let state = viewerState("plot");
    await page.route("**/api/projects/project/artifacts", (route) => route.fulfill({ json: [] }));
    await page.route("**/api/projects/project/artifacts/plot/state", (route) =>
      route.fulfill({ json: state }),
    );
    await page.route("**/api/projects/project/artifacts/plot/undo", (route) => {
      assert.equal(route.request().method(), "POST");
      state = viewerState("plot");
      return route.fulfill({ json: {} });
    });
    await page.route("**/viewer-shell/plot?*", (route) =>
      route.fulfill({
        contentType: "text/html",
        body: `<button id="send" onclick="parent.postMessage({type:'rcp-artifact-edit-started', version:1, artifact_id:'plot', operation_id:'edit'}, location.origin)">Send</button><p>${new URL(route.request().url()).searchParams.get("version")}</p>`,
      }),
    );
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/artifacts.html`,
    );
    const open = () =>
      page.evaluate(async () => {
        const { openArtifact } = await import("/src/artifacts/artifactViewer.ts");
        openArtifact({ projectId: "project", artifactId: "plot" });
      });
    await open();
    const panel = page.locator(".artifact-viewer");
    await panel.locator("iframe").waitFor();
    const docked = await panel.boundingBox();
    assert.equal(docked.y, 0);
    assert.equal(docked.height, 850);
    assert.equal(docked.x + docked.width, 1200);
    const separator = page.getByRole("separator", { name: "Viewer width" });
    const edge = await separator.boundingBox();
    await page.mouse.move(edge.x + edge.width / 2, edge.y + 200);
    await page.mouse.down();
    await page.mouse.move(edge.x - 80, edge.y + 200, { steps: 6 });
    await page.mouse.up();
    assert.ok((await panel.boundingBox()).width > docked.width);
    await page.getByRole("button", { name: "Dock viewer" }).click();
    const tab = page.getByRole("button", { name: "Restore Saved plot" });
    const tabRect = await tab.boundingBox();
    assert.ok(tabRect.y > 0, "The dock tab clears the project header");
    assert.equal(tabRect.x + tabRect.width, 1200);
    await tab.click();
    const title = panel.locator("header strong");
    await title.waitFor();
    const titleRect = await title.boundingBox();
    await page.mouse.move(titleRect.x + 20, titleRect.y + 10);
    await page.mouse.down();
    await page.mouse.move(titleRect.x - 60, titleRect.y + 80, { steps: 6 });
    await page.mouse.up();
    const floating = await panel.boundingBox();
    const placement = await page.evaluate(() =>
      JSON.parse(localStorage.getItem("rcp:artifact-viewer-placement")),
    );
    assert.equal(placement.mode, "floating");
    await title.dblclick();
    assert.deepEqual(await panel.boundingBox(), { x: 0, y: 0, width: 1200, height: 850 });
    await title.dblclick();
    assert.deepEqual(await panel.boundingBox(), floating);
    await page.getByRole("button", { name: "Close viewer" }).click();
    await open();
    await panel.locator("iframe").waitFor();
    assert.deepEqual(await panel.boundingBox(), floating);

    state = { ...state, editing_operation_id: "edit" };
    await page
      .frameLocator(".artifact-viewer iframe")
      .getByRole("button", { name: "Send" })
      .click();
    await panel.getByRole("status").waitFor();
    state = viewerState("plot", 2);
    await page.frameLocator(".artifact-viewer iframe").getByText("2", { exact: true }).waitFor();
    await panel.getByRole("status").waitFor({ state: "detached" });
    await panel.getByRole("button", { name: "Undo" }).click();
    await page.frameLocator(".artifact-viewer iframe").getByText("1", { exact: true }).waitFor();
    await panel.getByRole("button", { name: "Undo" }).waitFor({ state: "detached" });
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("desktop reports download and run PDFs open by stored identity without a producing task", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch();
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript(() => {
      window.nativeCalls = [];
      window.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
      window.__TAURI_INTERNALS__ = {
        transformCallback: () => 1,
        unregisterCallback() {},
        invoke: async (command, args) => {
          window.nativeCalls.push({ command, args });
          return { saved: true, path: "/tmp/report.html", opened: true };
        },
      };
    });
    await page.route("**/api/projects/project/artifacts", (route) =>
      route.fulfill({
        json: [
          {
            id: "report",
            artifact_id: "report",
            operation_id: null,
            name: "Report.html",
            view: "html",
            can_open: false,
            can_download: true,
            download_url: "/api/projects/project/artifacts/report/download",
          },
        ],
      }),
    );
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/artifacts.html`,
    );
    await page.getByRole("button", { name: "Download Report.html" }).click();
    await page.waitForFunction(() =>
      window.nativeCalls.some((call) => call.command === "download_artifact"),
    );
    assert.deepEqual(
      await page.evaluate(() =>
        window.nativeCalls.find((call) => call.command === "download_artifact"),
      ),
      {
        command: "download_artifact",
        args: { projectId: "project", artifactId: "report", suggestedName: "Report.html" },
      },
    );
    await page.evaluate(async () => {
      const { renderRunArtifacts } = await import("/tests/fixtures/artifacts.tsx");
      renderRunArtifacts([
        {
          artifact_id: "pdf",
          name: "Results.pdf",
          view: "pdf",
          supplier: "turn",
          origin_operation_id: null,
          created_at: new Date().toISOString(),
        },
      ]);
    });
    await page.getByRole("button", { name: "Results.pdf" }).click();
    await page.waitForFunction(() =>
      window.nativeCalls.some((call) => call.command === "open_artifact_pdf"),
    );
    assert.deepEqual(
      await page.evaluate(() =>
        window.nativeCalls.find((call) => call.command === "open_artifact_pdf"),
      ),
      {
        command: "open_artifact_pdf",
        args: { projectId: "project", artifactId: "pdf" },
      },
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
