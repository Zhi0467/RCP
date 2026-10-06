import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("chat question answers stay separate from steering and resolved cards enter the transcript", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const questions = ["single", "multiple", "dismiss"].map((id) => ({
      question_id: id,
      owner_kind: "chat",
      owner_id: "steering-chat",
      operation_id: "task",
      capability: "work_auto",
      question: id,
      choices: ["a", "b"],
      multiple: id === "multiple",
      state: "pending",
      answer: null,
      chosen_choices: [],
      resolved_by: null,
      resolved_at: null,
      withdrawn_readonly: false,
      can_answer: true,
      created_at: "2026-09-05T12:00:01Z",
    }));
    const mutations = [];
    let questionReads = 0;
    await page.route("**/api/projects/project/chats/steering-chat/questions", (route) => {
      questionReads += 1;
      return route.fulfill({ json: questions });
    });
    await page.route("**/api/projects/project/questions/*/*", (route) => {
      const [, id, action] = route
        .request()
        .url()
        .match(/questions\/([^/]+)\/(answer|dismiss)$/);
      const body = route.request().postDataJSON();
      mutations.push({ id, action, body });
      const question = questions.find((item) => item.question_id === id);
      Object.assign(question, {
        state: action === "answer" ? "answered" : "dismissed",
        answer: body.answer ?? null,
        chosen_choices: body.choices ?? [],
        can_answer: false,
      });
      return route.fulfill({ json: question });
    });
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/liveSteering.html`,
    );
    const single = page.locator('[data-question-id="single"]');
    await single.waitFor();
    assert.equal(questionReads, 1);
    await page.evaluate(async () => {
      window.setSteeringFixture({ elapsed_seconds: 6 });
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    });
    // Let any unwanted read finish before comparing; elapsed time is display-only.
    await page.waitForTimeout(150);
    assert.equal(questionReads, 1);
    const changedOwnerRead = page.waitForResponse(
      "**/api/projects/project/chats/steering-chat/questions",
    );
    await page.evaluate(() => window.setSteeringFixture({ updated_at: "2026-09-05T12:00:02Z" }));
    await changedOwnerRead;
    assert.equal(questionReads, 2);
    await single.getByRole("button", { name: "a", exact: true }).click();
    await page
      .locator('.node-chat-lines [data-question-id="single"][data-question-state="answered"]')
      .waitFor();
    const multi = page.locator('[data-question-id="multiple"]');
    await multi.getByRole("button", { name: "a", exact: true }).click();
    await multi.getByRole("button", { name: "b", exact: true }).click();
    assert.equal(mutations.length, 1);
    await multi.getByRole("textbox").fill("detail");
    await multi.getByRole("button", { name: "Answer", exact: true }).click();
    await page.locator('.node-chat-lines [data-question-id="multiple"]').waitFor();
    await page
      .locator('[data-question-id="dismiss"]')
      .getByRole("button", { name: "Dismiss", exact: true })
      .click();
    await page.locator('.node-chat-lines [data-question-id="dismiss"]').waitFor();
    assert.deepEqual(mutations, [
      { id: "single", action: "answer", body: { answer: "", choices: ["a"] } },
      { id: "multiple", action: "answer", body: { answer: "detail", choices: ["a", "b"] } },
      { id: "dismiss", action: "dismiss", body: {} },
    ]);
    assert.equal(await page.locator(".node-chat-lines .question-card textarea").count(), 0);
    assert.equal(
      await page.getByRole("textbox", { name: "Message", exact: true }).inputValue(),
      "",
    );
    assert.equal(
      await page.getByRole("button", { name: "Steer running turn", exact: true }).isEnabled(),
      false,
    );
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    // A project heartbeat refreshes parked questions even when tasks stop changing.
    questions.push({
      ...questions[0],
      question_id: "parked",
      state: "parked",
      can_answer: true,
      answer: null,
      chosen_choices: [],
    });
    await page.evaluate(() =>
      window.dispatchEvent(
        new CustomEvent("rcp:refresh-questions", { detail: "/api/projects/other" }),
      ),
    );
    assert.equal(await page.locator('[data-question-id="parked"]').count(), 0);
    await page.evaluate(() =>
      window.dispatchEvent(
        new CustomEvent("rcp:refresh-questions", { detail: "/api/projects/project" }),
      ),
    );
    await page.locator('[data-question-id="parked"]').waitFor();
    await page.evaluate(() =>
      window.setSteeringFixture({
        status: "succeeded",
        active: false,
        finished: true,
        can_steer: false,
        steer_visible: false,
      }),
    );
    await page
      .locator('[data-question-id="parked"]')
      .getByRole("button", { name: "Answer and continue Work", exact: true })
      .waitFor();
    questions.push({ ...questions[3], question_id: "discuss", capability: "discuss" });
    await page.evaluate(() =>
      window.dispatchEvent(
        new CustomEvent("rcp:refresh-questions", { detail: "/api/projects/project" }),
      ),
    );
    const discuss = page.locator('[data-question-id="discuss"]');
    await discuss.getByRole("textbox").fill("Discuss this route");
    await discuss.getByRole("button", { name: "Answer and continue Discuss", exact: true }).click();
    await page.locator('.node-chat-lines [data-question-id="discuss"]').waitFor();
    assert.deepEqual(mutations.at(-1), {
      id: "discuss",
      action: "answer",
      body: { answer: "Discuss this route", choices: [] },
    });
    questions[3].withdrawn_readonly = true;
    questions[3].can_answer = false;
    await page.evaluate(() =>
      window.dispatchEvent(
        new CustomEvent("rcp:refresh-questions", { detail: "/api/projects/project" }),
      ),
    );
    await page
      .locator('.node-chat-lines [data-question-id="parked"][data-question-state="ended"]')
      .waitFor();
    assert.equal(await page.locator('[data-question-id="parked"] textarea').count(), 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
