import assert from "node:assert/strict";
import test from "node:test";
import { browserReason } from "../src/browserStatus.ts";

const codes = [
  "ready",
  "not_installed",
  "node_missing",
  "node_too_old",
  "npm_missing",
  "system_libraries_missing",
  "unsupported_platform",
  "host_unreachable",
  "account_mismatch",
  "storage_unavailable",
  "owner_unavailable",
  "cleanup_pending",
  "runtime_failed",
  "capacity",
  "start_failed",
  "session_unreachable",
  "owner_mismatch",
  "capability_denied",
  "admission_failed",
  "lease_unknown",
  "finish_failed",
  "lost",
  "runtime_error",
  "command_failed",
];
test("browser reasons cover all wire codes and safely handle unknown codes", () => {
  for (const code of codes) {
    const reason = browserReason(code);
    assert.equal(reason.code, code);
    assert.ok(reason.label.length > 0);
    assert.ok(reason.reason.length > 0);
  }
  for (const code of [null, undefined, "new_server_code", "__proto__", "toString"]) {
    assert.equal(browserReason(code).code, "unknown");
  }
});

test("browser notices render only degraded turns and preserve their structured status", async () => {
  const { createServer } = await import("vite");
  const React = (await import("react")).default;
  const { renderToStaticMarkup } = await import("react-dom/server");
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    configFile: false,
    logLevel: "silent",
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true },
  });
  try {
    const { BrowserTurnNotice, BrowserToggle } = await server.ssrLoadModule(
      "/src/components/BrowserControls.tsx",
    );
    for (const status of [null, "not_requested", "granted", "unavailable", "lost"]) {
      const markup = renderToStaticMarkup(
        React.createElement(BrowserTurnNotice, {
          status: status ? { status, reason_code: "new_server_code", detail: null } : null,
        }),
      );
      if (status === "unavailable" || status === "lost") {
        assert.ok(markup.includes(`data-browser-status="${status}"`));
        assert.equal(markup.match(/role="status"/g)?.length, 1);
      } else assert.equal(markup, "");
    }
    const toggle = renderToStaticMarkup(
      React.createElement(BrowserToggle, { checked: false, disabled: true, onChange() {} }),
    );
    assert.match(toggle, /role="switch"/);
    assert.match(toggle, /aria-describedby="[^"]+"/);
    assert.match(toggle, /disabled=""/);
    assert.doesNotMatch(toggle, /checked=""/);
  } finally {
    await server.close();
  }
});
