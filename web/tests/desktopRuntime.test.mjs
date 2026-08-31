import assert from "node:assert/strict";
import test from "node:test";

import { isMutationRequest } from "../src/api.ts";
import {
  backendReconnectLabel,
  desktopDownloadPath,
  desktopFolderSelectionPath,
  desktopFolderAccessAcknowledgementValue,
  discardDesktopProjectTransferExport,
  establishBackendIdentity,
  exportDesktopProjectTransfer,
  finishDesktopProjectTransfer,
  identityMismatch,
  needsDesktopFolderAccessAcknowledgement,
  openDesktopProjectTransferTerminal,
  openEpisodeReportFromLink,
  reverifyBackendIdentity,
  runDesktopProjectTransfer,
  setDesktopWebviewZoom,
} from "../src/desktopRuntime.ts";

const identity = {
  version: "0.3.0",
  instance_id: "instance-a",
  data_dir_id: "data-a",
};

test("backend identity accepts the exact same process contract", () => {
  assert.equal(identityMismatch(identity, { ...identity }), null);
});

test("backend identity reports every changed contract field", () => {
  const message = identityMismatch(identity, {
    version: "0.4.0",
    instance_id: "instance-b",
    data_dir_id: "data-b",
  });
  assert.match(message, /version 0\.3\.0 became 0\.4\.0/);
  assert.match(message, /instance instance-a became instance-b/);
  assert.match(message, /data directory data-a became data-b/);
});

test("prepare-show bootstraps after the frontend outruns the desktop host", async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  let statusCalls = 0;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        status: "ok",
        ...identity,
        pid: 42,
        owner_kind: "desktop",
        active_agent_tasks: 0,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  const desktopWindow = new EventTarget();
  desktopWindow.__TAURI_INTERNALS__ = {
    invoke: async (command) => {
      assert.equal(command, "desktop_status");
      statusCalls += 1;
      if (statusCalls === 1) throw new Error("RCP is still starting");
      return {
        desktop: true,
        base_url: "http://127.0.0.1:8421",
        owner_kind: "desktop",
        active_agent_tasks: 0,
        owned: false,
        ...identity,
      };
    },
  };
  globalThis.window = desktopWindow;
  try {
    assert.equal((await establishBackendIdentity()).ok, false);
    assert.equal((await reverifyBackendIdentity("prepare-show")).ok, true);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
});

test("startup acceptance is not lost to an in-flight prepare-show verification", async () => {
  const originalFetch = globalThis.fetch;
  const replacementIdentity = {
    version: "0.3.0",
    instance_id: "instance-b",
    data_dir_id: "data-b",
  };
  let releaseHealth;
  const healthReady = new Promise((resolve) => {
    releaseHealth = resolve;
  });
  globalThis.fetch = async () => {
    await healthReady;
    return new Response(
      JSON.stringify({
        status: "ok",
        ...replacementIdentity,
        pid: 42,
        owner_kind: "desktop",
        active_agent_tasks: 0,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  };
  try {
    const prepareShow = reverifyBackendIdentity("prepare-show");
    const startup = establishBackendIdentity();
    releaseHealth();

    assert.equal((await prepareShow).ok, false);
    assert.equal((await startup).ok, true);
    assert.equal((await reverifyBackendIdentity("after-startup")).ok, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a desktop host that disagrees with health stops the window, however familiar health looks", async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const served = { version: "0.3.0", instance_id: "instance-b", data_dir_id: "data-b" };
  let shell = served;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        status: "ok",
        ...served,
        pid: 42,
        owner_kind: "desktop",
        active_agent_tasks: 0,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  const desktopWindow = new EventTarget();
  desktopWindow.__TAURI_INTERNALS__ = {
    invoke: async () => ({
      desktop: true,
      base_url: "http://127.0.0.1:8421",
      owner_kind: "desktop",
      active_agent_tasks: 0,
      owned: true,
      ...shell,
    }),
  };
  globalThis.window = desktopWindow;
  try {
    assert.equal((await establishBackendIdentity()).ok, true);
    shell = { version: "0.3.0", instance_id: "instance-a", data_dir_id: "data-a" };
    const result = await reverifyBackendIdentity("shell-disagrees");
    assert.equal(result.ok, false);
    assert.match(result.message, /instance instance-a became instance-b/);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
});

test("only requests that can mutate state trigger failure verification", () => {
  assert.equal(isMutationRequest(), false);
  assert.equal(isMutationRequest({ method: "GET" }), false);
  assert.equal(isMutationRequest({ method: "HEAD" }), false);
  assert.equal(isMutationRequest({ method: "post" }), true);
  assert.equal(isMutationRequest({ method: "DELETE" }), true);
});

test("closing the desktop save dialog is a normal artifact download cancel", () => {
  assert.equal(desktopDownloadPath({ saved: false, path: null }), null);
  assert.equal(desktopDownloadPath({ saved: true, path: "/tmp/report.png" }), "/tmp/report.png");
  assert.throws(() => desktopDownloadPath({ saved: false, error: "write failed" }), /write failed/);
});

test("closing the folder picker preserves the path while a selection returns its absolute path", () => {
  assert.equal(desktopFolderSelectionPath({ selected: false, path: null }), null);
  assert.equal(
    desktopFolderSelectionPath({ selected: true, path: "/Users/example/research project" }),
    "/Users/example/research project",
  );
  assert.throws(
    () => desktopFolderSelectionPath({ selected: true, path: null }),
    /did not return a repository folder/,
  );
});

test("desktop backend recovery uses a truthful native action label", () => {
  assert.equal(backendReconnectLabel(true), "Start or reconnect");
  assert.equal(backendReconnectLabel(false), "Reconnect");
});

test("folder access acknowledgement gates only desktop and is versioned", () => {
  assert.equal(needsDesktopFolderAccessAcknowledgement(false, null), false);
  assert.equal(needsDesktopFolderAccessAcknowledgement(true, null), true);
  assert.equal(needsDesktopFolderAccessAcknowledgement(true, "not json"), true);
  assert.equal(needsDesktopFolderAccessAcknowledgement(true, JSON.stringify({ version: 0 })), true);
  assert.equal(
    needsDesktopFolderAccessAcknowledgement(true, desktopFolderAccessAcknowledgementValue()),
    false,
  );
});

test("webview zoom is a no-op outside the desktop runtime", async () => {
  const originalWindow = globalThis.window;
  try {
    delete globalThis.window;
    await setDesktopWebviewZoom(1.2);
  } finally {
    if (originalWindow !== undefined) globalThis.window = originalWindow;
  }
});

test("episode report links use the native preview only in the desktop shell", async () => {
  const originalWindow = globalThis.window;
  let prevented = 0;
  const invocations = [];
  const desktopWindow = new EventTarget();
  desktopWindow.__TAURI_INTERNALS__ = {
    invoke: async (command, args) => {
      invocations.push({ command, args });
      return { opened: true };
    },
  };
  globalThis.window = desktopWindow;
  try {
    assert.equal(
      await openEpisodeReportFromLink(
        { preventDefault: () => (prevented += 1) },
        { projectId: "project one", episodeId: "episode/one" },
      ),
      true,
    );
    assert.equal(prevented, 1);
    assert.deepEqual(invocations, [
      {
        command: "open_episode_report_preview",
        args: { projectId: "project one", episodeId: "episode/one" },
      },
    ]);

    delete globalThis.window;
    assert.equal(
      await openEpisodeReportFromLink(
        { preventDefault: () => (prevented += 1) },
        { projectId: "project one", episodeId: "episode/one" },
      ),
      false,
    );
    assert.equal(prevented, 1);
  } finally {
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
});

test("project transfer bindings keep the relay native and pass only public metadata", async () => {
  const originalWindow = globalThis.window;
  const invocations = [];
  const callbacks = [];
  const desktopWindow = new EventTarget();
  desktopWindow.__TAURI_INTERNALS__ = {
    transformCallback: (callback) => {
      callbacks.push(callback);
      return callbacks.length;
    },
    unregisterCallback: () => undefined,
    invoke: async (command, args) => {
      invocations.push({ command, args });
      if (command === "desktop_run_project_transfer") {
        return {
          request_id: "11111111-1111-4111-8111-111111111111",
          target_request_id: "22222222-2222-4222-8222-222222222222",
          target_space_id: "33333333-3333-4333-8333-333333333333",
          connection_id: "44444444-4444-4444-8444-444444444444",
          archive_sha256: "a".repeat(64),
          archive_size_bytes: 1,
          exit_code: 0,
          event_count: 3,
          proof_verified: true,
          cleanup_acknowledged: true,
        };
      }
      if (command === "desktop_export_project_transfer") {
        return {
          saved: false,
          request_id: "11111111-1111-4111-8111-111111111111",
          target_request_id: null,
          target_space_id: null,
          archive_sha256: null,
          archive_size_bytes: null,
          path: null,
        };
      }
      if (command === "desktop_finish_project_transfer") {
        return {
          request_id: "11111111-1111-4111-8111-111111111111",
          target_request_id: "22222222-2222-4222-8222-222222222222",
          target_space_id: "33333333-3333-4333-8333-333333333333",
          connection_id: "44444444-4444-4444-8444-444444444444",
          proof_verified: true,
          cleanup_acknowledged: true,
        };
      }
      if (command === "desktop_discard_project_transfer_export") {
        return {
          request_id: "11111111-1111-4111-8111-111111111111",
          removed: true,
          path: "/tmp/transfer.rcp-transfer",
        };
      }
      return { opened: true, argv: [], command: "" };
    },
  };
  globalThis.window = desktopWindow;
  try {
    const requestId = "11111111-1111-4111-8111-111111111111";
    const runResult = await runDesktopProjectTransfer(requestId, () => undefined);
    assert.equal(runResult.proof_verified, true);
    await exportDesktopProjectTransfer(requestId);
    await openDesktopProjectTransferTerminal(requestId, "/tmp/transfer.rcp-transfer");
    await finishDesktopProjectTransfer(requestId, "/tmp/transfer.rcp-transfer");
    await discardDesktopProjectTransferExport(requestId, "/tmp/transfer.rcp-transfer");

    assert.deepEqual(
      invocations.map(({ command, args }) => ({
        command,
        keys: Object.keys(args),
        requestId: args.requestId,
        archivePath: args.archivePath,
      })),
      [
        {
          command: "desktop_run_project_transfer",
          keys: ["requestId", "onEvent"],
          requestId,
          archivePath: undefined,
        },
        {
          command: "desktop_export_project_transfer",
          keys: ["requestId"],
          requestId,
          archivePath: undefined,
        },
        {
          command: "desktop_open_project_transfer_terminal",
          keys: ["requestId", "archivePath"],
          requestId,
          archivePath: "/tmp/transfer.rcp-transfer",
        },
        {
          command: "desktop_finish_project_transfer",
          keys: ["requestId", "archivePath"],
          requestId,
          archivePath: "/tmp/transfer.rcp-transfer",
        },
        {
          command: "desktop_discard_project_transfer_export",
          keys: ["requestId", "archivePath"],
          requestId,
          archivePath: "/tmp/transfer.rcp-transfer",
        },
      ],
    );
    assert.equal(JSON.stringify(invocations[0].args).includes("archive_bytes"), false);
    assert.equal(JSON.stringify(invocations[0].args).includes("proof_bytes"), false);
  } finally {
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
});
