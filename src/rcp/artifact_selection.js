// Embedded inside RCP's private preview closure, before any artifact scripts.
function installArtifactSelection(surface, publish) {
  const doc = surface.ownerDocument || surface;
  const view = doc.defaultView;
  const capture = surface === doc ? doc.documentElement : surface;
  const listen = Function.prototype.call.bind(EventTarget.prototype.addEventListener);
  let drag = null;
  let mark = null;
  let anchor = null;
  let areaGesture = false;
  const utf8 = new TextEncoder();
  function bounded(value, limit) {
    let result = "";
    for (const character of String(value || "")
      .replace(/\s+/g, " ")
      .trim()) {
      if (utf8.encode(result + character).byteLength > limit) break;
      result += character;
    }
    return result;
  }

  function endDrag() {
    const id = drag?.id;
    drag = null;
    if (id !== undefined && capture.hasPointerCapture(id)) capture.releasePointerCapture(id);
  }

  function clear() {
    mark?.remove();
    mark = null;
    anchor = null;
    endDrag();
  }

  function textAtPoint(event) {
    // Preserve native highlighting, but padding around text is still drawable.
    if (event.target instanceof SVGElement) return false;
    for (const child of event.target.childNodes) {
      if (child.nodeType !== Node.TEXT_NODE || !child.textContent.trim()) continue;
      const range = doc.createRange();
      range.selectNodeContents(child);
      for (const rect of range.getClientRects()) {
        if (
          event.clientX >= rect.left &&
          event.clientX <= rect.right &&
          event.clientY >= rect.top &&
          event.clientY <= rect.bottom
        )
          return true;
      }
    }
    return false;
  }

  function bounds() {
    return surface === doc
      ? { left: 0, top: 0, width: view.innerWidth, height: view.innerHeight }
      : surface.getBoundingClientRect();
  }

  listen(
    surface,
    "pointerdown",
    (event) => {
      if (
        !event.isTrusted ||
        event.button !== 0 ||
        !event.isPrimary ||
        event.pointerType === "touch"
      )
        return;
      endDrag();
      areaGesture = false;
      if (
        event.target.closest(
          'a,button,input,textarea,select,summary,[contenteditable],[draggable="true"]',
        ) ||
        textAtPoint(event)
      )
        return;
      event.preventDefault();
      areaGesture = true;
      drag = {
        id: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        started: false,
        element: event.target,
      };
      capture.setPointerCapture(event.pointerId);
    },
    true,
  );

  listen(
    surface,
    "pointermove",
    (event) => {
      if (!event.isTrusted || !drag || event.pointerId !== drag.id) return;
      const width = Math.abs(event.clientX - drag.x),
        height = Math.abs(event.clientY - drag.y);
      if (!drag.started && Math.min(width, height) < 4) return;
      event.preventDefault();
      if (!drag.started) {
        drag.started = true;
        anchor = null;
        mark?.remove();
        doc.getSelection()?.removeAllRanges();
        publish(null);
        mark = doc.createElement("div");
        mark.dataset.rcpSelection = "area";
        mark.setAttribute("aria-hidden", "true");
        Object.assign(mark.style, {
          position: "fixed",
          zIndex: "2147483647",
          pointerEvents: "none",
          border: "2px solid #bd5b36",
          background: "rgba(189,91,54,.12)",
        });
        doc.documentElement.appendChild(mark);
      }
      Object.assign(mark.style, {
        left: `${Math.min(drag.x, event.clientX)}px`,
        top: `${Math.min(drag.y, event.clientY)}px`,
        width: `${width}px`,
        height: `${height}px`,
      });
    },
    true,
  );

  listen(
    surface,
    "pointerup",
    (event) => {
      if (!event.isTrusted || !drag || event.pointerId !== drag.id) return;
      const area = bounds();
      const left = Math.max(area.left, Math.min(drag.x, event.clientX));
      const top = Math.max(area.top, Math.min(drag.y, event.clientY));
      const right = Math.min(area.left + area.width, Math.max(drag.x, event.clientX));
      const bottom = Math.min(area.top + area.height, Math.max(drag.y, event.clientY));
      const drawn = drag.started;
      const element = drag.element;
      endDrag();
      if (!drawn) return;
      if (right - left < 4 || bottom - top < 4) {
        clear();
        return;
      }
      Object.assign(mark.style, {
        left: `${left}px`,
        top: `${top}px`,
        width: `${right - left}px`,
        height: `${bottom - top}px`,
      });
      anchor = { element, bounds: element.getBoundingClientRect(), left, top };
      event.preventDefault();
      const labels = new Set();
      if (surface === doc) {
        for (let xi = 0; xi <= 5; xi++)
          for (let yi = 0; yi <= 5; yi++) {
            let element = doc.elementFromPoint(
              left + ((right - left) * xi) / 5,
              top + ((bottom - top) * yi) / 5,
            );
            for (let depth = 0; element && depth < 3; depth++, element = element.parentElement) {
              if (["HTML", "BODY", "HEAD", "STYLE", "SCRIPT"].includes(element.tagName)) continue;
              const text = bounded(element.getAttribute("aria-label") || element.textContent, 512);
              if (text) {
                labels.add(text);
                break;
              }
            }
          }
      }
      publish({
        kind: "box",
        rect: {
          x: (left - area.left) / area.width,
          y: (top - area.top) / area.height,
          width: (right - left) / area.width,
          height: (bottom - top) / area.height,
        },
        viewport: { width: area.width, height: area.height },
        labels: bounded([...labels].join(" | "), 4096),
      });
    },
    true,
  );
  if (surface === doc)
    listen(doc, "mouseup", (event) => {
      if (!event.isTrusted) return;
      if (areaGesture) {
        areaGesture = false;
        return;
      }
      const selection = doc.getSelection();
      const text = bounded(selection?.toString(), 4096);
      if (!text || !selection?.rangeCount) return;
      clear();
      const range = selection.getRangeAt(0);
      const container =
        range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
          ? range.commonAncestorContainer
          : range.commonAncestorContainer.parentElement;
      publish({ kind: "text", text, surrounding_text: bounded(container?.textContent, 6144) });
    });
  listen(surface, "pointercancel", clear, true);
  listen(capture, "lostpointercapture", () => {
    if (drag) clear();
  });
  listen(view, "blur", () => {
    if (drag) clear();
  });
  listen(
    view,
    "scroll",
    () => {
      if (drag?.started) {
        clear();
        publish(null);
      } else {
        endDrag();
        if (mark && anchor) {
          const current = anchor.element.getBoundingClientRect();
          mark.style.left = `${anchor.left + current.left - anchor.bounds.left}px`;
          mark.style.top = `${anchor.top + current.top - anchor.bounds.top}px`;
        }
      }
    },
    true,
  );
  listen(doc, "keydown", (event) => {
    if (event.key === "Escape") {
      clear();
      publish(null);
    }
  });
  return clear;
}

function installSelectionConfirmation(container, confirm, clearSelection) {
  let pending = null;
  const excerpt = container.querySelector(".excerpt");
  const accept = container.querySelector("[data-confirm]");
  const cancel = container.querySelector("[data-cancel]");
  function offer(selection) {
    pending = selection;
    container.hidden = !selection;
    excerpt.textContent =
      selection?.kind === "text" ? selection.text : selection?.labels || "Selected area";
  }
  accept.addEventListener("click", () => {
    if (!pending) return;
    const selection = pending;
    clearSelection();
    offer(null);
    confirm(selection);
  });
  cancel.addEventListener("click", () => {
    clearSelection();
    offer(null);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      clearSelection();
      offer(null);
    }
  });
  return offer;
}
