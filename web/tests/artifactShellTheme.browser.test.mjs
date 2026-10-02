import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { chromium } from "playwright";

const shell = execFileSync(
  "uv",
  [
    "run",
    "python",
    "-c",
    `from rcp.artifacts import AgentArtifactDescriptor
from rcp.artifact_comments import comment_panel
from rcp.artifact_views import artifact_viewer_document
d=AgentArtifactDescriptor(artifact_id="a"*24,name="r.html",media_type="text/html",size_bytes=1)
print(artifact_viewer_document(d,content_url="/preview",state="task",panel=comment_panel({"projectId":"p","artifactId":"a"*24,"mediaType":"text/html"}))[0])`,
  ],
  { cwd: new URL("../..", import.meta.url), encoding: "utf8" },
);

test("the artifact shell paints with the containing app's theme and follows a switch", async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    await page.route("http://rcp.test/app", (route) =>
      route.fulfill({
        contentType: "text/html",
        body: '<html data-theme="classic" data-color-mode="dark"><iframe src="/viewer"></iframe></html>',
      }),
    );
    await page.route("http://rcp.test/viewer", (route) =>
      route.fulfill({ contentType: "text/html", body: shell }),
    );
    await page.route("**/{preview,state}", (route) => route.fulfill({ status: 404, body: "" }));
    await page.goto("http://rcp.test/app");
    const frame = page.frame({ url: "http://rcp.test/viewer" }) ?? page.frames()[1];
    await frame.waitForLoadState();
    const painted = () => frame.evaluate(() => ({ ...document.documentElement.dataset }));
    assert.deepEqual(await painted(), { theme: "classic", colorMode: "dark" });
    await page.evaluate(() => {
      document.documentElement.dataset.theme = "aqua";
      document.documentElement.dataset.colorMode = "light";
    });
    await frame.waitForFunction(() => document.documentElement.dataset.theme === "aqua");
    assert.deepEqual(await painted(), { theme: "aqua", colorMode: "light" });
  } finally {
    await browser.close();
  }
});
