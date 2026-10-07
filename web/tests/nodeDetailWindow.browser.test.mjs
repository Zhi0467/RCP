import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

// The node detail window's markup, as DetailDrawer and RelationMap render it,
// laid out by the real stylesheet.
let server;
let browser;
let origin;
before(async () => {
  server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  await server.listen();
  origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});

function nodeWindow(standing) {
  const contest = standing === "contested" ? " selected disagree" : "";
  const agree = standing === "accepted" ? " selected agree" : "";
  const peer = (title) => `<div class="relation-map-peer">
      <button type="button" class="relation-map-node"><span class="eyebrow">Evidence</span>
        <strong>${title}</strong><span class="standing asserted">asserted</span></button>
      <div class="relation-map-edges"><div class="relation-map-edge">
        <span class="relation-map-edge-arrow">↓</span>
        <span class="relation-map-edge-label">Supports</span></div></div>
    </div>`;
  return `<div class="floating-window node-detail-window" style="inset: 12px">
    <aside class="detail-drawer node-detail-drawer">
      <header data-drag-handle="true"><div><span class="eyebrow">Claim</span>
        <h2>A focused claim</h2></div></header>
      <div class="drawer-content">
        ${"<section><p>Field text.</p></section>".repeat(30)}
        <section><div class="relation-map relation-map-compact">
          <div class="relation-map-toolbar"><span>3 relations</span></div>
          <div class="relation-flow">
            <div class="relation-flow-level relation-flow-incoming">
              ${peer("Incoming evidence one")}${peer("Incoming evidence two")}
            </div>
            <div class="relation-map-node is-focused"><span class="eyebrow">Claim</span>
              <strong>${"The focused claim with a long title that wraps. ".repeat(3)}</strong>
              <span class="standing ${standing}">${standing}</span></div>
            <div class="relation-flow-level relation-flow-outgoing">${peer("Outgoing")}</div>
          </div>
        </div></section>
        ${"<section><p>Field text.</p></section>".repeat(30)}
      </div>
      <footer class="drawer-actions">
        <button class="button ghost">Ask about this node</button>
        <div class="node-detail-actions">
          <div class="node-judgment-actions">
            <button class="button secondary">Edit node</button>
            <button class="button judgment node-standing-toggle contest${contest}">Contest</button>
            <button class="button judgment node-standing-toggle agree${agree}">Agree</button>
          </div>
          <div class="node-removal-action"><button class="button danger">Remove node…</button></div>
        </div>
      </footer>
    </aside>
  </div>`;
}

async function stylesPage(viewport) {
  const page = await browser.newPage({ viewport, reducedMotion: "reduce" });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${origin}/tests/fixtures/stylesOnly.html`);
  await page.waitForFunction(() => document.documentElement.dataset.stylesReady === "true");
  await page.waitForFunction(
    () => getComputedStyle(document.body).backgroundColor !== "rgba(0, 0, 0, 0)",
  );
  return { page, errors };
}

function luminance(color) {
  const match = color.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)/);
  if (!match) return null;
  const [r, g, b] = match.slice(1, 4).map((value) => {
    const channel = Number(value) / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

test("selected Agree and Contest stay legible in both themes and color modes", async () => {
  const { page, errors } = await stylesPage({ width: 390, height: 844 });
  try {
    for (const theme of ["aqua", "classic"]) {
      for (const mode of ["light", "dark"]) {
        for (const standing of ["accepted", "contested"]) {
          await page.evaluate(
            ({ theme, mode, markup }) => {
              document.documentElement.dataset.theme = theme;
              document.documentElement.dataset.colorMode = mode;
              document.body.innerHTML = markup;
            },
            { theme, mode, markup: nodeWindow(standing) },
          );
          const selected = page.locator(".node-standing-toggle.selected");
          for (const state of ["rest", "hover"]) {
            if (state === "hover") await selected.hover();
            else await page.mouse.move(0, 0);
            const colors = await selected.evaluate((element) => {
              const style = getComputedStyle(element);
              return { color: style.color, background: style.backgroundColor };
            });
            const label = `${theme} ${mode} ${standing} ${state}: ${JSON.stringify(colors)}`;
            assert.notEqual(colors.color, colors.background, label);
            const ink = luminance(colors.color);
            const fill = luminance(colors.background);
            if (ink !== null && fill !== null) {
              const contrast = (Math.max(ink, fill) + 0.05) / (Math.min(ink, fill) + 0.05);
              assert.ok(contrast >= 3, `${label} contrast ${contrast.toFixed(2)}`);
            }
          }
        }
      }
    }
    assert.deepEqual(errors, []);
  } finally {
    await page.close();
  }
});

test("the focused relation card scrolls beneath the node actions, never over them", async () => {
  for (const viewport of [
    { width: 390, height: 844 },
    { width: 1280, height: 800 },
  ]) {
    const { page, errors } = await stylesPage(viewport);
    try {
      await page.evaluate((markup) => {
        document.documentElement.dataset.theme = "aqua";
        document.documentElement.dataset.colorMode = "light";
        document.body.innerHTML = markup;
      }, nodeWindow("accepted"));
      // Scroll whichever box scrolls the fields until the focused card sits
      // behind the footer's buttons.
      const covered = await page.evaluate(() => {
        const footer = document.querySelector(".drawer-actions");
        const card = document.querySelector(".relation-map-node.is-focused");
        const scroller = [card.closest(".drawer-content"), card.closest(".detail-drawer")].find(
          (element) => element.scrollHeight > element.clientHeight + 1,
        );
        const removal = document.querySelector(".node-removal-action .button");
        const target = removal.getBoundingClientRect();
        const box = card.getBoundingClientRect();
        scroller.scrollTop += box.top + box.height / 2 - (target.top + target.height / 2);
        const after = card.getBoundingClientRect();
        const point = target.top + target.height / 2;
        const overlaps = after.top < point && after.bottom > point;
        const probes = [...footer.querySelectorAll(".button")].map((button) => {
          const rect = button.getBoundingClientRect();
          const hit = document.elementFromPoint(
            rect.left + rect.width / 2,
            rect.top + rect.height / 2,
          );
          return footer.contains(hit) ? "footer" : `${hit?.className}`;
        });
        return { overlaps, probes };
      });
      assert.equal(covered.overlaps, true, `${viewport.width}px: card reaches the footer`);
      assert.deepEqual(
        covered.probes,
        covered.probes.map(() => "footer"),
        `${viewport.width}px footer hit targets`,
      );
      assert.deepEqual(errors, []);
    } finally {
      await page.close();
    }
  }
});
