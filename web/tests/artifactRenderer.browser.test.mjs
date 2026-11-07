import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { chromium } from "playwright";

const source = String.raw`# Math and formatting

**A result** with $h_0$ and \(h_1\).

$$
h_t = \mathrm{Mix}(h_0,h_{t-1})
$$

\[
x = y^2
\]

\$escaped\$ and unmatched $open.

Literal \`$code$\`.

\`\`\`tex
$$ code_only $$
\`\`\`

[Reference](https://example.invalid/private) and ![Diagram](https://example.invalid/chart.svg).

<script>window.authorExecuted=true; parent.document.body.dataset.compromised='yes'</script>

</script><img src=x onerror="window.authorExecuted=true">

| Item | Value |
| --- | --- |
| math | $z_2$ |
`.replaceAll("\\`", "`");

function renderedMarkdown() {
  return JSON.parse(
    execFileSync(
      "uv",
      [
        "run",
        "--no-sync",
        "python",
        "-c",
        `
import json
from rcp.artifact_views import artifact_content, artifact_viewer_document
from rcp.artifacts import AgentArtifactDescriptor
source=${JSON.stringify(source)}
body, media, csp = artifact_content('math.md','text/markdown',source.encode())
descriptor = AgentArtifactDescriptor(artifact_id='a'*24,name='math.md',media_type='text/markdown',size_bytes=len(source))
shells={}
for presentation in ['panel','inline']:
    shell, policy=artifact_viewer_document(descriptor,content_url='/content',state='temporary',presentation=presentation)
    shells[presentation]={'body':shell,'csp':policy}
print(json.dumps({'body':body,'csp':csp,'shells':shells}))
`,
      ],
      {
        cwd: new URL("../..", import.meta.url),
        encoding: "utf8",
        maxBuffer: 32 * 1024 * 1024,
        env: { ...process.env, PYTHONPATH: "src" },
      },
    ).trim(),
  );
}

test("Markdown artifacts render chat math in both opaque presentations without author capabilities", async (t) => {
  const rendered = renderedMarkdown();
  const browser = await chromium.launch();
  t.after(() => browser.close());
  for (const presentation of ["panel", "inline"]) {
    for (const width of [1280, 390]) {
      await t.test(`${presentation} at ${width}px`, async () => {
        const page = await browser.newPage({ viewport: { width, height: 900 } });
        const requests = [];
        const errors = [];
        page.on("request", (request) => requests.push(request.url()));
        page.on("pageerror", (error) => errors.push(error.message));
        await page.route("http://rcp.test/app", (route) =>
          route.fulfill({
            contentType: "text/html",
            body: '<body style="margin:0"><iframe src="/viewer" style="border:0;width:100%;height:800px"></iframe></body>',
          }),
        );
        await page.route("http://rcp.test/viewer", (route) =>
          route.fulfill({
            contentType: "text/html",
            body: rendered.shells[presentation].body,
            headers: { "Content-Security-Policy": rendered.shells[presentation].csp },
          }),
        );
        await page.route("http://rcp.test/content?**", (route) =>
          route.fulfill({
            contentType: "text/html",
            body: rendered.body,
            headers: { "Content-Security-Policy": rendered.csp },
          }),
        );
        await page.route("http://rcp.test/content", (route) =>
          route.fulfill({
            contentType: "text/html",
            body: rendered.body,
            headers: { "Content-Security-Policy": rendered.csp },
          }),
        );
        await page.goto("http://rcp.test/app");
        const content = page
          .frames()
          .find((frame) => new URL(frame.url() || "about:blank").pathname === "/content");
        assert.ok(content);
        await content.locator(".katex").first().waitFor({ timeout: 3000 });
        assert.equal(await content.locator(".katex").count(), 5);
        assert.equal(await content.locator(".katex-display").count(), 2);
        assert.equal(await content.locator("strong").innerText(), "A result");
        assert.equal(await content.locator("th").count(), 2);
        assert.match(
          await content
            .locator("code")
            .allTextContents()
            .then((text) => text.join(" ")),
          /\$code\$/,
        );
        assert.equal(await content.locator("a,img,form,iframe,object").count(), 0);
        assert.equal(await content.evaluate(() => Boolean(window.authorExecuted)), false);
        assert.equal(
          await content.evaluate(() => {
            try {
              return Boolean(parent.document);
            } catch {
              return false;
            }
          }),
          false,
        );
        assert.equal(await page.locator("body").getAttribute("data-compromised"), null);
        assert.ok(requests.every((url) => url.startsWith("http://rcp.test/")));
        assert.deepEqual(errors, []);
        await page.close();
      });
    }
  }
});

function renderedHtml() {
  return JSON.parse(
    execFileSync(
      "uv",
      [
        "run",
        "--no-sync",
        "python",
        "-c",
        String.raw`
import json
from rcp.artifact_renderer import paper_math_addon
from rcp.artifact_views import artifact_content, inline_viewer_document
page = rb'''<!doctype html><html><body>
<p id="inline">$h_0$ and \(h_1\)</p>
<div id="display">$$ x = y^2 $$ and \[ z = x_0 \]</div>
<p id="escaped">\$cash\$ and unmatched $open</p>
<code id="code">$code$ and \(literal\)</code><pre>$$ pre $$</pre>
<script>window.authoredValue='$script$';</script><style>#ignored{--literal:'$style$'}</style>
<p id="bad">$\frac$</p>
<p id="unsafe">$\href{javascript:alert(1)}{click}$</p>
</body></html>'''
body, media, csp=artifact_content('page.html','text/html',page,frame_addon=paper_math_addon(['Missing local figure: fixture.svg']))
shell, shell_csp=inline_viewer_document('page.html','html',content_url='/content',live=None,selectable=True,follow_appearance=False)
print(json.dumps({'body':body,'csp':csp,'shell':shell,'shellCsp':shell_csp}))
`,
      ],
      {
        cwd: new URL("../..", import.meta.url),
        encoding: "utf8",
        maxBuffer: 32 * 1024 * 1024,
        env: { ...process.env, PYTHONPATH: "src" },
      },
    ).trim(),
  );
}

test("Paper HTML math shares fonts and options, preserves authored code and reports warnings without theme reloads", async (t) => {
  const rendered = renderedHtml();
  const browser = await chromium.launch();
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 390, height: 900 } });
  const contentRequests = [];
  const external = [];
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (!request.url().startsWith("http://rcp.test/")) external.push(request.url());
  });
  await page.route("http://rcp.test/app", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: '<html data-theme="aqua" data-color-mode="light"><body><iframe src="/viewer" style="border:0;width:100%;height:800px"></iframe><script>window.received=[];addEventListener("message",e=>received.push(e.data))</script></body></html>',
    }),
  );
  await page.route("http://rcp.test/viewer", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: rendered.shell,
      headers: { "Content-Security-Policy": rendered.shellCsp },
    }),
  );
  await page.route("http://rcp.test/content?**", (route) => {
    contentRequests.push(route.request().url());
    return route.fulfill({
      contentType: "text/html",
      body: rendered.body,
      headers: { "Content-Security-Policy": rendered.csp },
    });
  });
  await page.goto("http://rcp.test/app");
  const shell = page.frame({ url: "http://rcp.test/viewer" });
  const wrapper = shell.childFrames()[0];
  const artifact = wrapper.childFrames()[0];
  await artifact.locator("#inline .katex").first().waitFor();
  assert.equal(await artifact.locator("#inline .katex").count(), 2);
  assert.equal(await artifact.locator("#display .katex-display").count(), 2);
  assert.equal(await artifact.locator("#code .katex, pre .katex").count(), 0);
  assert.equal(await artifact.locator("#code").innerText(), "$code$ and \\(literal\\)");
  assert.equal(await artifact.evaluate(() => window.authoredValue), "$script$");
  assert.equal(await artifact.locator("#bad .katex-error").count(), 1);
  assert.equal(await artifact.locator("#unsafe a").count(), 0);
  await artifact.evaluate(() => document.fonts.ready);
  assert.ok(
    await artifact.evaluate(() =>
      [...document.fonts].some(
        (font) => font.status === "loaded" && font.family.startsWith("KaTeX"),
      ),
    ),
  );
  await page.waitForFunction(() => received.some((value) => value.kind === "rcp-artifact-error"));
  assert.match(
    await page.evaluate(
      () => received.find((value) => value.kind === "rcp-artifact-error").message,
    ),
    /fixture\.svg/,
  );
  const before = contentRequests.length;
  await artifact.evaluate(() => {
    document.body.dataset.state = "retained";
  });
  await page.evaluate(() => {
    document.documentElement.dataset.theme = "classic";
    document.documentElement.dataset.colorMode = "dark";
  });
  await shell.waitForFunction(() => document.documentElement.dataset.theme === "classic");
  assert.equal(contentRequests.length, before);
  assert.equal(await artifact.locator("body").getAttribute("data-state"), "retained");
  assert.deepEqual(external, []);
  assert.deepEqual(errors, []);
});
