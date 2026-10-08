// The viewer shell inside a reply. `inlineConfig` is defined by the shell document.
// The shell sizes itself to its content and tells the containing chat its height;
// comment mode forwards selections to the chat, which stages them in its composer.
const inlineRoot = document.documentElement;
const inlineFrame = document.getElementById("preview");
const inlineImage = document.getElementById("previewImage");
const inlineOrigin = window.location.origin;
const toChat = (value) => {
  if (window.parent !== window) window.parent.postMessage(value, inlineOrigin);
};
const clampHeight = (value) =>
  Math.max(1, Math.min(inlineConfig.maxHeight, Math.ceil(value)));

// The content renders with the reply's appearance, so a theme switch reloads it.
let inlineAppearance = "";
function loadInlineContent() {
  if (!inlineFrame) return;
  const next = `${inlineRoot.dataset.theme}/${inlineRoot.dataset.colorMode}`;
  if (next === inlineAppearance) return;
  inlineAppearance = next;
  const url = new URL(inlineConfig.contentUrl, window.location.href);
  url.searchParams.set("presentation", "inline");
  url.searchParams.set("theme", inlineRoot.dataset.theme || "aqua");
  url.searchParams.set("color_mode", inlineRoot.dataset.colorMode || "light");
  inlineFrame.style.height = `${inlineConfig.initialHeight}px`;
  inlineFrame.src = url.pathname + url.search;
}
loadInlineContent();
new MutationObserver(loadInlineContent).observe(inlineRoot, {
  attributes: true,
  attributeFilter: ["data-theme", "data-color-mode"],
});

let lastShellHeight = -1;
function reportShellHeight() {
  const height = Math.ceil(inlineRoot.getBoundingClientRect().height);
  if (height === lastShellHeight) return;
  lastShellHeight = height;
  toChat({ type: "rcp-artifact-size", version: 1, height });
}
new ResizeObserver(reportShellHeight).observe(inlineRoot);

// Comment mode: off until the chat asks, so a game or a chart reacts to the pointer.
let commentMode = false;
let clearImageSelection = null;
let imageLayer = null;
const bounded = (value, limit) =>
  String(value || "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, limit);
const unit = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
function boundedSelection(raw) {
  if (!raw || typeof raw !== "object") return null;
  if (raw.kind === "text" && typeof raw.text === "string" && raw.text.trim())
    return {
      kind: "text",
      text: bounded(raw.text, 4096),
      surrounding_text: bounded(raw.surrounding_text, 6144),
    };
  if (
    raw.kind === "box" &&
    raw.rect &&
    ["x", "y", "width", "height"].every((key) => unit(raw.rect[key])) &&
    raw.viewport &&
    Number.isFinite(raw.viewport.width) &&
    Number.isFinite(raw.viewport.height)
  )
    return {
      kind: "box",
      rect: {
        x: raw.rect.x,
        y: raw.rect.y,
        width: raw.rect.width,
        height: raw.rect.height,
      },
      viewport: {
        width: Math.max(1, Math.min(32768, Math.round(raw.viewport.width))),
        height: Math.max(1, Math.min(32768, Math.round(raw.viewport.height))),
      },
      elements: (Array.isArray(raw.elements) ? raw.elements : []).slice(0, 8).map((element) => ({
        path: bounded(element?.path, 512) || "body",
        label: bounded(element?.label, 256),
        text: bounded(element?.text, 512),
        ...(element?.region &&
        ["x", "y", "width", "height"].every((key) => unit(element.region[key]))
          ? {
              region: {
                x: element.region.x,
                y: element.region.y,
                width: element.region.width,
                height: element.region.height,
              },
            }
          : {}),
      })),
    };
  return null;
}
function offerSelection(raw) {
  const selection = raw === null ? null : boundedSelection(raw);
  if (raw !== null && !selection) return;
  toChat({
    type: "rcp-artifact-selection",
    version: 1,
    selection,
    description: selection ? describeSelection(selection) : "",
  });
}
function applyCommentMode() {
  if (inlineFrame?.contentWindow)
    inlineFrame.contentWindow.postMessage(
      { type: commentMode ? "rcp-artifact-selection-enable" : "rcp-artifact-selection-disable" },
      "*",
    );
  if (imageLayer) imageLayer.hidden = !commentMode;
  if (!commentMode) clearImageSelection?.();
}
if (inlineImage && inlineConfig.selectable) {
  imageLayer = document.createElement("div");
  imageLayer.id = "boxLayer";
  imageLayer.hidden = true;
  imageLayer.setAttribute("aria-hidden", "true");
  inlineImage.parentElement.append(imageLayer);
  // The image is shown whole at its own aspect, so a box over the layer is
  // already a fraction of the image; the viewport names its natural size.
  clearImageSelection = installArtifactSelection(
    imageLayer,
    (selection) =>
      offerSelection(
        selection && {
          ...selection,
          viewport: {
            width: inlineImage.naturalWidth || selection.viewport.width,
            height: inlineImage.naturalHeight || selection.viewport.height,
          },
        },
      ),
    () => commentMode,
  );
}
if (inlineFrame) inlineFrame.addEventListener("load", () => commentMode && applyCommentMode());

window.addEventListener("message", (event) => {
  const value = event.data;
  if (!value || typeof value !== "object") return;
  if (inlineFrame && event.source === inlineFrame.contentWindow) {
    if (value.type === "rcp-artifact-size" && value.version === 1 && Number.isFinite(value.height))
      inlineFrame.style.height = `${clampHeight(value.height)}px`;
    else if (
      inlineConfig.selectable &&
      commentMode &&
      value.type === "rcp-artifact-selection" &&
      value.version === 1 &&
      "selection" in value
    )
      offerSelection(value.selection);
    return;
  }
  if (event.source !== window.parent || event.origin !== inlineOrigin) return;
  if (value.type === "rcp-inline-comment-mode" && value.version === 1) {
    commentMode = inlineConfig.selectable && value.enabled === true;
    applyCommentMode();
  } else if (value.type === "rcp-inline-selection-clear" && value.version === 1) {
    if (inlineFrame?.contentWindow)
      inlineFrame.contentWindow.postMessage({ type: "rcp-artifact-selection-clear" }, "*");
    clearImageSelection?.();
  }
});
