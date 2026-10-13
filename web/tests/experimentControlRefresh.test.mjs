import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { ResourceStore } = await server.ssrLoadModule("/src/core/resourceStore.ts");
const { taskStatusProfile, selectTasks } = await server.ssrLoadModule(
  "/src/agents/taskResources.ts",
);
after(() => server.close());
const flush = () => new Promise((resolve) => setImmediate(resolve));
function manual(project = "p", name = "tasks.status") {
  const reads = [];
  return {
    reads,
    profile: {
      ...taskStatusProfile(project),
      key: { ...taskStatusProfile(project).key, name },
      fetch: () => new Promise((resolve, reject) => reads.push({ resolve, reject })),
    },
  };
}
const task = (status) => ({ operation_id: "task", status, updated_at: "1" });

test("retained task tab state copies selections without task payloads", async () => {
  const { cloneAgentTasksSnapshot } = await server.ssrLoadModule("/src/agents/useAgentTasks.ts");
  const selected = task("failed");
  const snapshot = {
    tasks: [selected],
    retryTask: selected,
    inspectedTask: selected,
    taskInspectorId: "inspected",
    activityTaskId: "activity",
  };
  assert.deepEqual(cloneAgentTasksSnapshot(snapshot), {
    retryTaskId: "task",
    taskInspectorId: "inspected",
    activityTaskId: "activity",
  });
  assert.equal(cloneAgentTasksSnapshot({ ...snapshot, retryTask: null }).retryTaskId, null);
});

test("branch-retained views share project task state and one completion reader", async () => {
  const store = new ResourceStore();
  const { profile, reads } = manual();
  const main = store.register("main", profile);
  const branch = store.register("branch", profile);
  assert.equal(main, branch);
  assert.equal(reads.length, 1);
  reads[0].resolve([task("running")]);
  await flush();
  store.observe("p", [{ name: "tasks.status", version: "done" }]);
  assert.equal(reads.length, 2);
  reads[1].resolve([task("succeeded")]);
  await flush();
  assert.equal(store.view(branch).value[0].status, "succeeded");
});

test("activation during a hidden task read accepts it without a restore write", async () => {
  const store = new ResourceStore();
  const { profile, reads } = manual();
  const id = store.register("tab", profile);
  store.setActiveProject("p");
  reads[0].resolve([task("succeeded")]);
  await flush();
  assert.equal(reads.length, 1);
  assert.equal(store.view(id).value[0].status, "succeeded");
});

test("task read failure does not block independent resources", async () => {
  const store = new ResourceStore();
  const tasks = manual();
  const usage = manual("p", "usage");
  store.setActiveProject("p");
  const taskId = store.register("tab", tasks.profile);
  const usageId = store.register("tab", usage.profile);
  tasks.reads[0].reject(new Error("offline"));
  usage.reads[0].resolve({ total: 42 });
  await flush();
  assert.equal(store.view(taskId).stale, true);
  assert.deepEqual(store.view(usageId).value, { total: 42 });
});

test("pending rollback preserves newer state and fences competing completion reads", async () => {
  const store = new ResourceStore();
  const { profile, reads } = manual();
  const id = store.register("tab", profile);
  reads[0].resolve([task("running")]);
  await flush();
  store.invalidate(id);
  const mutation = store.beginMutation(
    "pause",
    new Map([[id, (rows) => rows.map((row) => ({ ...row, paused: true }))]]),
  );
  assert.equal(store.view(id).value[0].paused, true);
  store.write(id, [task("succeeded")]);
  mutation.fail();
  reads[1].resolve([task("running")]);
  await flush();
  assert.equal(store.view(id).value[0].status, "succeeded");
  assert.equal(store.view(id).value[0].paused, undefined);
  assert.deepEqual(store.view(id).pending, []);
});

test("progress cannot regress status or overwrite a pending action", () => {
  const status = { ...task("succeeded"), paused: true, progress: 0 };
  const progress = { ...task("running"), paused: false, progress: 1, updated_at: "2" };
  const [selected] = selectTasks([status], [progress]);
  assert.equal(selected.status, "succeeded");
  assert.equal(selected.paused, true);
  assert.equal(selected.progress, 0);
  assert.equal(selectTasks([status], [{ ...progress, status: "succeeded" }])[0].progress, 1);
  assert.deepEqual(selectTasks([], [progress]), []);
});

test("task commands paint intent, settle a retry and retain failure on the task", async () => {
  const { beginTaskMutation } = await server.ssrLoadModule("/src/agents/taskActions.ts");
  const { ApiError } = await server.ssrLoadModule("/src/core/api.ts");
  const store = new ResourceStore();
  const { profile, reads } = manual();
  const id = store.register("tab", profile);
  const original = { ...task("running"), can_pause: true };
  reads[0].resolve([original]);
  await flush();
  const pause = beginTaskMutation(store, "p", original, "pause");
  assert.equal(store.view(id).value[0].pausing, true);
  store.write(id, [{ ...original, status: "succeeded", updated_at: "2" }]);
  pause.fail(new ApiError("refused", 409));
  assert.equal(store.view(id).value[0].status, "succeeded");
  assert.equal(store.view(id).value[0].pausing, undefined);
  assert.ok(store.view(id).value[0].action_error);
  const refreshed = { ...original, status: "succeeded", updated_at: "3" };
  for (let settled = 0; settled < reads.length; settled += 1) {
    reads[settled].resolve([refreshed]);
    await flush();
  }
  assert.equal(store.view(id).value[0].updated_at, "3");
  assert.equal(store.view(id).value[0].action_error_action, "pause");
  assert.ok(store.view(id).value[0].action_error);
  assert.equal(store.committedValue(id)[0].action_error, undefined);
  assert.deepEqual(store.view(id).pending, []);
  // The next action clears feedback; a lost answer then shows no failure.
  const lost = beginTaskMutation(store, "p", original, "resume");
  assert.equal(store.view(id).value[0].action_error, undefined);
  lost.fail(new TypeError("response lost"));
  assert.deepEqual(store.view(id).pending, []);
  assert.notEqual(store.view(id).value[0].action_error_action, "resume");
  const admitted = { ...original, status: "succeeded", updated_at: "4" };
  for (let settled = 0; settled < reads.length; settled += 1) {
    reads[settled].resolve([admitted]);
    await flush();
  }
  assert.equal(store.view(id).value[0].updated_at, "4");
  assert.equal(store.view(id).value[0].action_error, undefined);
  const retry = beginTaskMutation(store, "p", original, "retry");
  assert.equal(store.view(id).value[0].action_error, undefined);
  retry.settle({ ...task("queued"), operation_id: "retry" });
  assert.deepEqual(
    store.view(id).value.map((row) => row.operation_id),
    ["retry", "task"],
  );
  assert.equal(store.view(id).value[1].status, "succeeded");
  assert.deepEqual(store.view(id).pending, []);
  const closed = beginTaskMutation(store, "p", original, "pause");
  store.release("tab");
  store.register("tab", profile);
  reads.at(-1).resolve([admitted]);
  await flush();
  closed.fail(new ApiError("refused", 409));
  assert.equal(store.view(id).value[0].action_error, undefined);
});

async function chatStore() {
  const store = new ResourceStore();
  store.setActiveProject("p");
  const tasks = manual();
  const taskId = store.register("tab", tasks.profile);
  const chat = manual("p", "chat");
  chat.profile.key = { ...chat.profile.key, entity: "c", query: "branch:b" };
  const chatId = store.register("tab", chat.profile);
  tasks.reads[0].resolve([]);
  chat.reads[0].resolve({ messages: [{ message_id: "old", text: "Earlier" }] });
  await flush();
  return { store, taskId, chatId, chat };
}

test("ambiguous chat sends retain admission key and resend settles one card", async () => {
  const { beginChatSend } = await server.ssrLoadModule("/src/agents/chatSendMutation.ts");
  const { ApiError } = await server.ssrLoadModule("/src/core/api.ts");
  const { store, taskId, chatId, chat } = await chatStore();
  const send = () =>
    beginChatSend(
      store,
      "p",
      { kind: "branch", branch_id: "b" },
      "project_chat",
      { chat_id: "c", message: "Hello" },
      "admission",
    );
  const first = send();
  first.fail(new TypeError("response lost"));
  assert.deepEqual(store.view(taskId).pending, ["admission"]);
  assert.equal(store.view(taskId).value[0].operation_id, "pending-admission");
  assert.equal(store.view(taskId).value[0].active, true);
  assert.equal(store.view(chatId).value.messages.at(-1).text, "Hello");
  const resend = send();
  resend.fail(new ApiError("unavailable", 503));
  assert.equal(store.view(taskId).value.length, 1);
  assert.equal(store.view(chatId).value.messages.length, 2);
  resend.settle({ ...task("queued"), operation_id: "accepted" });
  assert.deepEqual(
    store.view(taskId).value.map((row) => row.operation_id),
    ["accepted"],
  );
  assert.deepEqual(store.view(taskId).pending, []);
  assert.deepEqual(store.view(chatId).pending, []);
  chat.reads[1].resolve({ messages: [{ message_id: "accepted-message", text: "Hello" }] });
  await flush();
  assert.equal(store.view(chatId).value.messages.length, 1);
});

test("definite chat rejection removes task and transcript overlays", async () => {
  const { beginChatSend } = await server.ssrLoadModule("/src/agents/chatSendMutation.ts");
  const { ApiError } = await server.ssrLoadModule("/src/core/api.ts");
  const { store, taskId, chatId } = await chatStore();
  const send = beginChatSend(
    store,
    "p",
    { kind: "branch", branch_id: "b" },
    "project_chat",
    { chat_id: "c", message: "Hello" },
    "admission",
  );
  assert.equal(store.view(taskId).value.length, 1);
  send.fail(new ApiError("forbidden", 403));
  assert.deepEqual(store.view(taskId).value, []);
  assert.deepEqual(
    store.view(chatId).value.messages.map((message) => message.message_id),
    ["old"],
  );
  assert.deepEqual(store.view(chatId).pending, []);
});

test("settling one task command never persists another command's overlay", async () => {
  const { beginTaskMutation } = await server.ssrLoadModule("/src/agents/taskActions.ts");
  const store = new ResourceStore();
  const source = manual();
  const id = store.register("tab", source.profile);
  const first = task("running");
  const second = { ...task("running"), operation_id: "second" };
  source.reads[0].resolve([first, second]);
  await flush();
  const pauseFirst = beginTaskMutation(store, "p", first, "pause");
  const pauseSecond = beginTaskMutation(store, "p", second, "pause");
  pauseFirst.fail(new Error("refused"));
  assert.equal(store.view(id).value[1].action_pending, "pause");
  pauseSecond.fail(new Error("refused too"));
  assert.equal(store.view(id).value[1].action_pending, undefined);
  assert.equal(store.view(id).value[1].pausing, second.pausing);
  assert.deepEqual(store.view(id).pending, []);
});
