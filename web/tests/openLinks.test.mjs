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
after(() => server.close());

globalThis.window = { location: { pathname: "/open", hash: "#l=tok_en-1" } };
const model = await server.ssrLoadModule("/src/open/openLinkModel.ts");
delete globalThis.window;
const { installLinkGuard, leavesApp } = await server.ssrLoadModule("/src/core/linkGuard.ts");
const { OpenLinkCard } = await server.ssrLoadModule("/src/open/OpenLinkPage.tsx");
const { AppearancePicker } = await server.ssrLoadModule("/src/ui/AppearancePicker.tsx");
const { ApiError } = await server.ssrLoadModule("/src/core/api.ts");

const resolved = {
  project_id: "p",
  project_name: "Lens study",
  kind: "artifact",
  title: "Loss curves by seed",
  link: "#/projects/p/targets/main/artifact/a1",
};
const cardView = (overrides) =>
  model.openLinkCardView({
    start: "card",
    sessionKnown: true,
    signedIn: true,
    resolved,
    retry: false,
    fallback: false,
    offersApp: true,
    spaceName: "Lab",
    ...overrides,
  });
const card = (view) =>
  renderToStaticMarkup(
    React.createElement(OpenLinkCard, {
      view,
      remember: false,
      onRemember: () => undefined,
      onOpenInApp: () => undefined,
      onOpenHere: () => undefined,
      onRetry: () => undefined,
    }),
  );
const buttons = (html) => (html.match(/<button/g) ?? []).length;

test("the token is read once from /open and nowhere else", () => {
  assert.deepEqual(model.currentShareLinkRequest(), {
    token: "tok_en-1",
    inPlace: false,
    openHere: false,
  });
  assert.equal(model.shareTokenFromLocation("/", "#l=abc"), null);
  assert.equal(model.shareTokenFromLocation("/open", "#/projects/p"), null);
  assert.equal(model.shareTokenFromLocation("/open", "#l=a/b"), null);
});

test("signed in, the card names the item and offers both openings", () => {
  const view = cardView();
  assert.equal(view.kind, "artifact");
  assert.equal(view.name, resolved.title);
  assert.ok(view.title.includes(resolved.project_name));
  assert.deepEqual([view.app, view.here, view.remember], [true, true, true]);
  const html = card(view);
  assert.equal(buttons(html), 2);
  assert.match(html, /type="checkbox"/);
  assert.equal(cardView({ resolved: undefined }), null);
});

test("signed out, the card shows no item details and the token waits through sign-in", () => {
  const view = cardView({ signedIn: false, resolved: undefined });
  assert.equal(view.kind, null);
  assert.equal(view.name, null);
  assert.ok(!view.title.includes(resolved.project_name));
  assert.equal(view.here, true);
  const step = { desktop: false, openHere: true, choice: "ask", offersApp: true };
  assert.equal(model.openLinkStart({ ...step, signedIn: false }), "signin");
  assert.equal(model.openLinkStart({ ...step, signedIn: true }), "resolve");
  model.chooseOpenHere();
  assert.deepEqual(model.currentShareLinkRequest(), {
    token: "tok_en-1",
    inPlace: false,
    openHere: true,
  });
});

test("a link that does not resolve shows Not available with no actions", () => {
  for (const start of ["card", "resolve", "handoff"]) {
    const view = cardView({ start, resolved: null });
    assert.equal(view.unavailable, true);
    assert.deepEqual([view.app, view.here, view.remember], [false, false, false]);
    const html = card(view);
    assert.equal(buttons(html), 0);
    assert.match(html, /role="alert"/);
  }
});

test("each remembered choice picks its own start, and the desktop shell never shows the card", () => {
  const start = (choice, signedIn, desktop = false, offersApp = !desktop) =>
    model.openLinkStart({ desktop, signedIn, openHere: false, choice, offersApp });
  assert.equal(start("ask", true), "card");
  assert.equal(start("ask", false), "card");
  assert.equal(start("here", true), "resolve");
  assert.equal(start("here", false), "signin");
  assert.equal(start("app", true), "handoff");
  assert.equal(start("app", false), "handoff");
  assert.equal(start("app", true, false, false), "card");
  for (const choice of model.OPEN_CHOICES) {
    assert.equal(start(choice, true, true), "resolve");
    assert.equal(start(choice, false, true), "signin");
  }
});

test("the card waits until the session state is known", () => {
  assert.equal(cardView({ sessionKnown: false, signedIn: false, resolved: undefined }), null);
});

test("a dead link settles over the app on desktop or on error, and a busy server offers Retry", () => {
  assert.equal(model.openLinkFailure(false, null), "card");
  assert.equal(model.openLinkFailure(true, null), "notice");
  for (const desktop of [false, true]) {
    assert.equal(model.openLinkFailure(desktop, new ApiError("busy", 503)), "retry");
    assert.equal(model.openLinkFailure(desktop, new ApiError("boom", 500)), "notice");
    assert.equal(model.openLinkFailure(desktop, new TypeError("offline")), "notice");
  }
  const view = cardView({ retry: true, resolved: undefined });
  assert.deepEqual([view.app, view.here, view.remember, view.retry], [false, false, false, true]);
  assert.equal(buttons(card(view)), 1);
});

test("a skipped hand-off falls back to Open here", () => {
  assert.equal(cardView({ start: "handoff", resolved: undefined }), null);
  const view = cardView({ start: "handoff", resolved: undefined, fallback: true });
  assert.deepEqual([view.app, view.here, view.remember], [false, true, false]);
  assert.equal(buttons(card(view)), 1);
  const handoff = new URL(model.appHandoffUrl("space-1", "rcp.lab.example:8443", "tok"));
  assert.equal(handoff.protocol, "rcp:");
  assert.deepEqual(Object.fromEntries(handoff.searchParams), {
    space: "space-1",
    host: "rcp.lab.example:8443",
    l: "tok",
  });
});

test("Open in app is offered on a Mac browser only", () => {
  const mac = "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/605.1.15";
  assert.equal(model.offersOpenInApp(false, mac, 0), true);
  assert.equal(model.offersOpenInApp(true, mac, 0), false);
  assert.equal(model.offersOpenInApp(false, mac, 5), false);
  assert.equal(model.offersOpenInApp(false, "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", 0), false);
  assert.equal(cardView({ offersApp: false }).app, false);
});

test("the guard opens a same-space share link in place and any other in a tab", () => {
  const here = new URL("https://rcp.lab.example/#/projects/p");
  assert.equal(model.sameSpaceShareToken("https://rcp.lab.example/open#l=abc", here, null), "abc");
  const shell = new URL("http://127.0.0.1:47000/#/projects/p");
  assert.equal(
    model.sameSpaceShareToken(
      "https://rcp.lab.example/open#l=abc",
      shell,
      "https://rcp.lab.example",
    ),
    "abc",
  );
  assert.equal(model.sameSpaceShareToken("https://other.example/open#l=abc", shell, null), null);
  assert.equal(model.sameSpaceShareToken("https://rcp.lab.example/files#l=abc", here, null), null);
  assert.equal(leavesApp("https://rcp.lab.example/open#l=abc", here), true);

  let onClick;
  let onCapture;
  const opened = [];
  class Anchor {
    constructor(href) {
      this.href = href;
      this.target = "";
    }
    hasAttribute() {
      return false;
    }
  }
  globalThis.HTMLAnchorElement = Anchor;
  globalThis.document = { addEventListener: (_name, handler) => (onClick = handler) };
  globalThis.window = {
    location: { href: here.href },
    open: (href) => opened.push(href),
    addEventListener: (_name, handler) => (onCapture = handler),
  };
  try {
    installLinkGuard();
    const click = (href) => {
      const event = {
        defaultPrevented: false,
        button: 0,
        target: { closest: () => new Anchor(href) },
        stopped: false,
        preventDefault() {
          this.defaultPrevented = true;
        },
        stopImmediatePropagation() {
          this.stopped = true;
        },
      };
      onCapture(event);
      if (!event.stopped) onClick(event);
      return event.defaultPrevented;
    };
    assert.equal(click("https://rcp.lab.example/open#l=same"), true);
    assert.deepEqual(model.currentShareLinkRequest(), {
      token: "same",
      inPlace: true,
      openHere: true,
    });
    assert.deepEqual(opened, []);
    assert.equal(click("https://other.example/open#l=far"), true);
    assert.deepEqual(opened, ["https://other.example/open#l=far"]);
    assert.equal(model.currentShareLinkRequest()?.token, "same");
  } finally {
    model.settleShareLink();
    delete globalThis.window;
    delete globalThis.document;
    delete globalThis.HTMLAnchorElement;
  }
});

test("the Open share links preference row shows only where the card can show", () => {
  const picker = (openShareLinks, openInApp = true) =>
    renderToStaticMarkup(
      React.createElement(AppearancePicker, {
        themeChoice: "system",
        colorModeChoice: "system",
        onThemeChoiceChange: () => undefined,
        onColorModeChoiceChange: () => undefined,
        openShareLinks,
        openInApp,
      }),
    );
  const groups = (html) => (html.match(/role="group"/g) ?? []).length;
  const choices = (html) =>
    (
      html
        .split('aria-label="Open share links"')[1]
        .split("</div>")[0]
        .match(/<button/g) ?? []
    ).length;
  assert.equal(groups(picker(true)), groups(picker(false)) + 1);
  assert.match(picker(true), /aria-pressed="true"/);
  assert.equal(choices(picker(true, false)), choices(picker(true)) - 1);
  assert.equal(model.effectiveOpenChoice("app", false), "ask");
  assert.equal(model.effectiveOpenChoice("app", true), "app");
});
