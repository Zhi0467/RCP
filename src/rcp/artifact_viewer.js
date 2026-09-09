const selections = [];
const frame = document.getElementById("preview"),
  boxLayer = document.getElementById("boxLayer");
const items = document.getElementById("items"),
  empty = document.getElementById("empty"),
  add = document.getElementById("add"),
  notice = document.getElementById("notice");
const openChat = document.getElementById("open-chat");
const draftKey = `rcp:artifact-selections:${encodeURIComponent(config.projectId)}:${config.source}:${encodeURIComponent(config.operationId)}:${config.artifactId}`;
const bounded = (value, limit) =>
  String(value || "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, limit);
function saveSelections(added = false) {
  openChat.hidden = !added;
  try {
    localStorage.setItem(draftKey, JSON.stringify({ selections, added }));
  } catch {
    notice.textContent = "Comments could not be saved. Keep this preview open and try again.";
  }
}
function render() {
  items.replaceChildren();
  empty.hidden = selections.length > 0;
  add.disabled = selections.length === 0 || !config.chatAvailable;
  selections.forEach((selection, index) => {
    const card = document.createElement("section");
    card.className = "selection";
    const label = document.createElement("b");
    label.textContent = `${index + 1} · ${selection.kind}`;
    const excerpt = document.createElement("div");
    excerpt.className = "excerpt";
    excerpt.textContent =
      selection.kind === "text"
        ? selection.text
        : selection.labels ||
          `Box ${Math.round(selection.rect.x * 100)}–${Math.round((selection.rect.x + selection.rect.width) * 100)}%`;
    const comment = document.createElement("textarea");
    comment.placeholder = "Comment or question";
    comment.maxLength = 2048;
    comment.value = selection.comment || "";
    comment.addEventListener("input", () => {
      selection.comment = comment.value.slice(0, 2048);
      saveSelections();
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove selection ${index + 1}`);
    remove.addEventListener("click", () => {
      selections.splice(index, 1);
      saveSelections();
      render();
    });
    card.append(remove, label, excerpt, comment);
    items.append(card);
  });
}
try {
  const saved = JSON.parse(localStorage.getItem(draftKey) || "null");
  if (saved && Array.isArray(saved.selections)) {
    selections.push(...saved.selections.slice(0, 12));
    render();
    openChat.hidden = !saved.added;
  }
} catch {
  selections.length = 0;
  render();
  notice.textContent = "Saved comments could not be restored.";
}
function appendSelection(selection) {
  if (selections.length >= 12) {
    notice.textContent = "A prompt can include at most 12 selections.";
    return;
  }
  selections.push(selection);
  saveSelections();
  render();
  items.lastElementChild?.querySelector("textarea")?.focus();
}
let clearImageSelection = null;
const offerSelection = installSelectionConfirmation(
  document.getElementById("pending"),
  appendSelection,
  () => {
    if (frame) frame.contentWindow?.postMessage({ type: "rcp-artifact-selection-clear" }, "*");
    else clearImageSelection?.();
  },
);
window.addEventListener("message", (event) => {
  if (!frame || event.source !== frame.contentWindow) return;
  const value = event.data;
  if (
    !value ||
    value.type !== "rcp-artifact-selection" ||
    value.version !== 1 ||
    !("selection" in value)
  )
    return;
  const raw = value.selection;
  if (raw === null) {
    offerSelection(null);
    return;
  }
  if (raw.kind === "text" && typeof raw.text === "string")
    offerSelection({
      kind: "text",
      text: bounded(raw.text, 4096),
      surrounding_text: bounded(raw.surrounding_text, 6144),
      comment: "",
    });
  else if (raw.kind === "box" && raw.rect && raw.viewport)
    offerSelection({
      kind: "box",
      rect: raw.rect,
      viewport: raw.viewport,
      labels: bounded(raw.labels, 4096),
      comment: "",
    });
});
if (frame) {
  const enableSelection = () =>
    frame.contentWindow?.postMessage({ type: "rcp-artifact-selection-enable" }, "*");
  frame.addEventListener("load", enableSelection);
  enableSelection();
}
if (boxLayer) clearImageSelection = installArtifactSelection(boxLayer, offerSelection);
add.addEventListener("click", () => {
  if (!config.chatAvailable) {
    notice.textContent = "The originating chat is unavailable.";
    return;
  }
  const payload = {
    type: "rcp-artifact-context",
    version: 1,
    project_id: config.projectId,
    chat_id: config.chatId,
    operation_id: config.operationId,
    artifact_id: config.artifactId,
    artifact_name: config.artifactName,
    media_type: config.mediaType,
    selections,
  };
  payload.source = config.source;
  payload.episode_id = config.episodeId;
  const key = `rcp:artifact-context:${encodeURIComponent(config.projectId)}:${encodeURIComponent(config.chatId)}`;
  try {
    localStorage.setItem(key, JSON.stringify(payload));
  } catch {
    notice.textContent = "Could not add comments to the chat draft. Try again.";
    return;
  }
  try {
    const channel = new BroadcastChannel("rcp-artifact-context");
    channel.postMessage(payload);
    channel.close();
  } catch {}
  notice.textContent = "Added to the originating chat draft.";
  saveSelections(true);
});
openChat.addEventListener("click", (event) => {
  if (!("__TAURI_INTERNALS__" in window)) return;
  event.preventDefault();
  notice.textContent = "Opening chat…";
  try {
    const channel = new BroadcastChannel("rcp-artifact-chat-navigation");
    const requestId = crypto.randomUUID();
    const expiresAt = Date.now() + config.chatOpenTimeoutMs;
    const timeout = setTimeout(() => {
      channel.close();
      notice.textContent = "Open the originating RCP space, then try Open chat again.";
    }, config.chatOpenTimeoutMs);
    channel.onmessage = ({ data }) => {
      if (data?.requestId !== requestId) return;
      clearTimeout(timeout);
      channel.close();
      notice.textContent = data.error || "Opened the originating chat.";
    };
    channel.postMessage({
      requestId,
      hash: new URL(openChat.href).hash,
      expiresAt,
    });
  } catch {
    notice.textContent = "Could not reach the RCP window. Try Open chat again.";
  }
});
