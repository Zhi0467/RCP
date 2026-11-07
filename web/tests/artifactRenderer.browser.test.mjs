import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import {
  mkdtemp,
  mkdir,
  copyFile,
  writeFile,
  readFile,
  symlink,
  rm,
  realpath,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "vite";
import { chromium } from "playwright";

const source = String.raw`# Math and formatting

**A result** with $h_0$ and \(h_1\).

Unsafe $\href{javascript:alert(1)}{click}$.

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
        assert.equal(await content.locator(".katex").count(), 6);
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
        assert.ok(
          requests.every((url) => ["/app", "/viewer", "/content"].includes(new URL(url).pathname)),
        );
        await content.evaluate(() => document.fonts.ready);
        assert.ok(await content.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        assert.equal(await content.evaluate(() => typeof window.RcpArtifactRenderer), "undefined");
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
<p id="inside">$x+\$1$</p>
<p id="slashes">\\\$three\\\$ and unmatched $open</p>
<p id="escaped-double">\$$literal$$</p>
<p id="escaped-brackets">\\(literal\) and \\[literal\]</p>
<p id="four-slashes">\\\\(literal\) and \\\\[literal\]</p>
<p id="even-dollar">\\$e_2$ and \\$$d_2$$</p>
<p id="odd-brackets">\\\(e_3\) and \\\[d_3\]</p>
<p id="escaped-unmatched">\\(open and \\[open and \$$open</p>
<p id="inside-display">$$ x + \$1 $$</p>
<code id="code">$code$ and \(literal\) and $$display$$ and \[literal\] and \\(escaped\)</code><pre>$$ pre $$</pre>
<span id="existing" class="katex">$keep$ and \(literal\) and \$$literal$$</span>
<script>window.authoredValue='$script$';</script><style>#ignored{--literal:'$style$'}</style>
<p id="bad">$\frac$</p>
<p id="unsafe">$\href{javascript:alert(1)}{click}$</p>
</body></html>'''
page=page.replace(b'</body>', b'<p id="unmatched-slashes">'+bytes([92])*20_000+b'</p></body>')
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
  assert.equal(await artifact.locator("#inline .katex-display").count(), 0);
  assert.equal(await artifact.locator("#display .katex-display").count(), 2);
  assert.equal(await artifact.locator("#inside .katex").count(), 1);
  for (const [id, text] of [
    ["escaped-double", String.raw`\$$literal$$`],
    ["escaped-brackets", String.raw`\\(literal\) and \\[literal\]`],
    ["four-slashes", String.raw`\\\\(literal\) and \\\\[literal\]`],
    ["escaped-unmatched", String.raw`\\(open and \\[open and \$$open`],
  ]) {
    assert.equal(await artifact.locator(`#${id} .katex`).count(), 0, id);
    assert.equal(await artifact.locator(`#${id}`).innerText(), text);
  }
  assert.equal(await artifact.locator("#even-dollar .katex").count(), 2);
  assert.equal(await artifact.locator("#even-dollar .katex-display").count(), 1);
  assert.equal(await artifact.locator("#odd-brackets .katex").count(), 2);
  assert.equal(await artifact.locator("#odd-brackets .katex-display").count(), 1);
  assert.equal(
    await artifact.locator("#inside-display annotation").textContent(),
    String.raw` x + \$1 `,
  );
  assert.equal(await artifact.locator("#unmatched-slashes").innerText(), "\\".repeat(20_000));
  assert.equal(await artifact.locator("#slashes .katex").count(), 0);
  assert.equal(
    await artifact.locator("#slashes").innerText(),
    String.raw`\\\$three\\\$ and unmatched $open`,
  );
  assert.equal(await artifact.locator("#code .katex, pre .katex").count(), 0);
  assert.equal(await artifact.locator("#escaped .katex").count(), 0);
  assert.equal(await artifact.locator("#escaped").innerText(), "\\$cash\\$ and unmatched $open");
  assert.equal(
    await artifact.locator("#code").innerText(),
    String.raw`$code$ and \(literal\) and $$display$$ and \[literal\] and \\(escaped\)`,
  );
  assert.equal(
    await artifact.locator("#existing").textContent(),
    String.raw`$keep$ and \(literal\) and \$$literal$$`,
  );
  assert.equal(await artifact.locator("#existing .katex").count(), 0);
  assert.equal(await artifact.evaluate(() => window.authoredValue), "$script$");
  assert.equal(await artifact.evaluate(() => typeof window.RcpArtifactRenderer), "undefined");
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

function nextWatchBuild(watcher) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      watcher.off("event", listener);
      reject(new Error("Artifact watch build timed out"));
    }, 30000);
    const listener = (event) => {
      if (event.code !== "END" && event.code !== "ERROR") return;
      clearTimeout(timeout);
      watcher.off("event", listener);
      if (event.code === "ERROR") reject(event.error);
      else resolve();
    };
    watcher.on("event", listener);
  });
}

test("the existing Vite watch lifecycle rebuilds the packaged renderer and shared syntax", async (t) => {
  const root = await realpath(await mkdtemp(join(tmpdir(), "rcp-renderer-watch-")));
  let watcher;
  t.after(async () => {
    await watcher?.close();
    await rm(root, { recursive: true, force: true });
  });
  for (const path of ["build", "src/artifacts", "src/core"])
    await mkdir(join(root, path), { recursive: true });
  for (const path of [
    "build/artifactRenderer.mjs",
    "src/artifacts/artifactRenderer.tsx",
    "src/core/markdownSyntax.ts",
  ]) {
    await copyFile(fileURLToPath(new URL("../" + path, import.meta.url)), join(root, path));
  }
  await symlink(
    fileURLToPath(new URL("../node_modules", import.meta.url)),
    join(root, "node_modules"),
    "dir",
  );
  await writeFile(join(root, "package.json"), '{"type":"module"}');
  await writeFile(join(root, "index.html"), '<script type="module" src="/src/main.js"></script>');
  await writeFile(join(root, "src/main.js"), 'document.body.textContent="watch fixture";');
  const { artifactRenderer } = await import(
    pathToFileURL(join(root, "build/artifactRenderer.mjs"))
  );
  watcher = await build({
    root,
    configFile: false,
    logLevel: "silent",
    plugins: [artifactRenderer()],
    build: { watch: {} },
  });
  await nextWatchBuild(watcher);
  const script = join(root, "dist/artifact-renderer.js");
  const before = await readFile(script, "utf8");
  let ready = nextWatchBuild(watcher);
  const entry = join(root, "src/artifacts/artifactRenderer.tsx");
  await writeFile(
    entry,
    (await readFile(entry, "utf8")) + '\nexport const watchVersion = "updated";\n',
  );
  await ready;
  assert.notEqual(await readFile(script, "utf8"), before);
  assert.match(await readFile(script, "utf8"), /updated/);
  ready = nextWatchBuild(watcher);
  const syntax = join(root, "src/core/markdownSyntax.ts");
  await writeFile(
    syntax,
    (await readFile(syntax, "utf8")) + "\nexport const syntaxWatchProbe = true;\n",
  );
  await ready;
  const css = await readFile(join(root, "dist/artifact-renderer.css"), "utf8");
  assert.match(css, /url\(data:font\/woff2;base64,/);
  assert.doesNotMatch(css, /url\(fonts\//);
});
