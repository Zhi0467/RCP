const selections = [];
const frame = document.getElementById("preview");
const image = document.getElementById("previewImage");
const boxLayer = image && config.selectionEnabled ? document.createElement("div") : null;
if (boxLayer) {
  boxLayer.id = "boxLayer";
  boxLayer.setAttribute("aria-hidden", "true");
  image.parentElement.append(boxLayer);
  const sizeOverlay = () => {
    const scale = Math.min(image.clientWidth / image.naturalWidth, image.clientHeight / image.naturalHeight);
    const width = image.naturalWidth * scale, height = image.naturalHeight * scale;
    Object.assign(boxLayer.style, {left: `${(image.clientWidth-width)/2}px`, top: `${(image.clientHeight-height)/2}px`, width: `${width}px`, height: `${height}px`});
  };
  image.addEventListener("load", sizeOverlay);
  const observer = new ResizeObserver(sizeOverlay);
  observer.observe(image);
  if (image.complete && image.naturalWidth) sizeOverlay();
  window.addEventListener("pagehide", () => { observer.disconnect(); boxLayer.remove(); }, {once:true});
}
const items = document.getElementById("items"),
  count = document.getElementById("count"),
  tray = document.getElementById("tray"),
  queue = document.getElementById("queue"),
  editNow = document.getElementById("editNow"),
  send = document.getElementById("send"),
  general = document.getElementById("general"),
  notice = document.getElementById("notice");
const message = document.getElementById("message");
// Every comment is one object: its text, and the selection it is anchored to (or
// none, for the whole artifact). Edit now and Send post the same list.
const comments = [];
let current = null;
let viewerState = null;
let sending = false;
let sendError = "";
let stateLoading = false;
let stateTimer = null;
let stopped = false;
let permanentStateError = false;
let freshSessionRequired = false;
const retryState = document.createElement("button");
retryState.type = "button";
retryState.textContent = "Retry";
retryState.hidden = true;
notice.after(retryState);
retryState.addEventListener("click", () => {
  permanentStateError = false;
  retryState.hidden = true;
  clearTimeout(stateTimer);
  pollState();
});
const draftKey = `rcp:artifact-selections:${encodeURIComponent(config.projectId)}:${config.artifactId}`;
const bounded = (value, limit) =>
  String(value || "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, limit);
const anchoredCount = () => comments.filter((comment) => comment.selection).length;
function saveComments() {
  try {
    localStorage.setItem(draftKey, JSON.stringify({ comments, draft: message.value }));
  } catch {
    notice.textContent = "Comments could not be saved. Keep this preview open and try again.";
  }
}
function render() {
  items.replaceChildren();
  count.textContent = String(comments.length);
  count.hidden = comments.length === 0;
  comments.forEach((comment, index) => {
    const card = document.createElement("section");
    card.className = "selection";
    const label = document.createElement("b");
    label.textContent = `${index + 1} · ${comment.selection ? comment.selection.kind : "whole artifact"}`;
    const excerpt = document.createElement("div");
    excerpt.className = "excerpt";
    excerpt.textContent = comment.selection ? describeSelection(comment.selection) : "";
    const text = document.createElement("p");
    text.textContent = comment.text;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove comment ${index + 1}`);
    remove.addEventListener("click", () => {
      comments.splice(index, 1);
      saveComments();
      render();
    });
    card.append(remove, label, excerpt, text);
    items.append(card);
  });
  updateSend();
}
try {
  const saved = JSON.parse(localStorage.getItem(draftKey) || "null");
  // Drafts saved before comments were one object held selections and a message.
  const restored = Array.isArray(saved?.comments)
    ? saved.comments
    : [
        ...(Array.isArray(saved?.selections) ? saved.selections : [])
          .filter((selection) => selection?.comment)
          .map(({ comment, ...selection }) => ({ text: comment, selection })),
        ...(typeof saved?.message === "string" && saved.message.trim()
          ? [{ text: saved.message, selection: null }]
          : []),
      ];
  comments.push(
    ...restored
      .filter((comment) => typeof comment?.text === "string" && comment.text.trim())
      .slice(0, config.maxSelections),
  );
  message.value = typeof saved?.draft === "string" ? saved.draft : "";
  render();
} catch {
  comments.length = 0;
  render();
  notice.textContent = "Saved comments could not be restored.";
}
function addComment(selection) {
  const text = message.value.trim();
  if (!text) return false;
  const anchor = selection && selection.kind !== "whole" ? selection : null;
  if (anchor && anchoredCount() >= config.maxSelections) {
    notice.textContent = `A prompt can include at most ${config.maxSelections} selections.`;
    return false;
  }
  comments.push({ text: text.slice(0, 2048), selection: anchor });
  message.value = "";
  current = null;
  saveComments();
  render();
  return true;
}
let clearImageSelection = null;
const clearSelection = () => {
  if (frame) frame.contentWindow?.postMessage({ type: "rcp-artifact-selection-clear" }, "*");
  else clearImageSelection?.();
};
const offerComposer = installSelectionConfirmation(
  document.getElementById("composer"),
  addComment,
  clearSelection,
);
function offerSelection(selection) {
  current = selection;
  offerComposer(selection);
  if (selection) message.focus();
  updateSend();
}
general.addEventListener("click", () => offerSelection({ kind: "whole" }));
const dropCurrent = () => {
  current = null;
  updateSend();
};
document.getElementById("composer").querySelector("[data-cancel]").addEventListener("click", dropCurrent);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") dropCurrent();
});
window.addEventListener("message", (event) => {
  if (!config.selectionEnabled || !frame || event.source !== frame.contentWindow) return;
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
  else if (raw.kind === "box" && raw.rect && raw.viewport && Array.isArray(raw.elements))
    offerSelection({
      kind: "box",
      rect: raw.rect,
      viewport: raw.viewport,
      elements: raw.elements.slice(0, 8).map((element) => ({
        path: bounded(element?.path, 512) || "body",
        label: bounded(element?.label, 256),
        text: bounded(element?.text, 512),
        ...(element?.region ? { region: element.region } : {}),
      })),
      comment: "",
    });
});
if (frame && config.selectionEnabled) {
  const enableSelection = () =>
    frame.contentWindow?.postMessage({ type: "rcp-artifact-selection-enable" }, "*");
  frame.addEventListener("load", enableSelection);
  enableSelection();
}
// The layer spans the canvas while the image is letterboxed inside it, so an image box
// is re-expressed as a fraction of the image itself: that is what the server crops.
function imageSelection(selection) {
  const image = document.getElementById("previewImage");
  if (!selection || !image?.naturalWidth || !image.naturalHeight) return selection;
  const layer = boxLayer.getBoundingClientRect();
  const scale = Math.min(layer.width / image.naturalWidth, layer.height / image.naturalHeight);
  const width = image.naturalWidth * scale,
    height = image.naturalHeight * scale;
  const clamp = (value) => Math.min(1, Math.max(0, value));
  const left = clamp((selection.rect.x * layer.width - (layer.width - width) / 2) / width);
  const top = clamp((selection.rect.y * layer.height - (layer.height - height) / 2) / height);
  const right = clamp(
    ((selection.rect.x + selection.rect.width) * layer.width - (layer.width - width) / 2) / width,
  );
  const bottom = clamp(
    ((selection.rect.y + selection.rect.height) * layer.height - (layer.height - height) / 2) /
      height,
  );
  if (right - left <= 0 || bottom - top <= 0) return null;
  return {
    ...selection,
    rect: { x: left, y: top, width: right - left, height: bottom - top },
    viewport: {
      width: Math.min(32768, image.naturalWidth),
      height: Math.min(32768, image.naturalHeight),
    },
  };
}
if (boxLayer)
  clearImageSelection = installArtifactSelection(boxLayer, (selection) => {
    const mapped = selection && imageSelection(selection);
    offerSelection(mapped ? { ...mapped, comment: "" } : null);
  });
function updateSend() {
  const fresh = freshSessionRequired || viewerState?.fresh_session_required;
  const blocked = sending || !viewerState?.can_comment;
  const writing = Boolean(current && message.value.trim());
  queue.disabled = !writing;
  editNow.disabled = blocked || !writing;
  send.disabled = blocked || comments.length === 0;
  editNow.textContent = fresh ? "Edit now in a new session" : "Edit now";
  send.textContent = fresh ? "Send in a new session" : "Send to original chat";
}
async function refreshState() {
  if (stateLoading || stopped || permanentStateError || document.hidden) return;
  stateLoading = true;
  try {
    const response = await fetch(config.stateUrl, {credentials: "same-origin"});
    if (!response.ok) {
      permanentStateError = response.status >= 400 && response.status < 500 &&
        ![408, 409, 425, 429].includes(response.status);
      retryState.hidden = !permanentStateError;
      throw new Error("Comment availability could not be loaded.");
    }
    viewerState = await response.json();
    notice.textContent = sendError || viewerState.comment_unavailable_reason || "";
  } catch (error) {
    viewerState = null;
    notice.textContent = sendError || error.message;
  } finally {
    stateLoading = false;
  }
  updateSend();
}
message.addEventListener("input", () => { sendError = ""; saveComments(); updateSend(); });
async function postComments(now) {
  sendError = "";
  sending = true;
  updateSend();
  try {
    const response = await fetch(config.commentsUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        comments: comments.map(({ text, selection }) => ({ text, selection })),
        edit_now: now,
        fresh_session: freshSessionRequired || !!viewerState?.fresh_session_required,
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 409 && result.detail?.code === "fresh_session_required")
        freshSessionRequired = true;
      throw new Error(typeof result.detail === "string" ? result.detail :
        result.detail?.message || "Comment could not be sent.");
    }
    freshSessionRequired = false;
    comments.length = 0;
    saveComments();
    tray.open = false;
    render();
    window.parent.postMessage({type: "rcp-artifact-edit-started", version: 1,
      artifact_id: config.artifactId, operation_id: result.operation_id}, location.origin);
    await refreshState();
  } catch (error) {
    sendError = error.message;
    notice.textContent = sendError;
  } finally {
    sending = false;
    updateSend();
  }
}
// Edit now adds the comment being written to the list, then asks for the edit at once.
editNow.addEventListener("click", () => {
  if (editNow.disabled || !addComment(current)) return;
  clearSelection();
  offerComposer(null);
  void postComments(true);
});
send.addEventListener("click", () => {
  if (!send.disabled) void postComments(false);
});
window.addEventListener("focus", refreshState);
async function pollState() {
  await refreshState();
  if (!stopped && !permanentStateError) stateTimer = setTimeout(pollState, config.stateRefreshMs);
}
window.addEventListener("pagehide", () => { stopped = true; clearTimeout(stateTimer); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshState(); });
pollState();
