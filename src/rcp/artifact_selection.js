// Embedded inside RCP's private preview closure, before any artifact scripts.
// `isActive` lets a surface that is also played with turn the gestures off.
function installArtifactSelection(surface, publish, isActive = () => true) {
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

  // Name elements the way a reader of the HTML source can find them.
  function elementPath(element) {
    const parts = [];
    for (
      let node = element;
      node && node !== doc.body && node !== doc.documentElement && parts.length < 6;
      node = node.parentElement
    ) {
      const tag = node.tagName.toLowerCase();
      if (node.id) {
        parts.unshift(`${tag}#${CSS.escape(node.id)}`);
        break;
      }
      const same = node.parentElement
        ? [...node.parentElement.children].filter((child) => child.tagName === node.tagName)
        : [];
      parts.unshift(same.length > 1 ? `${tag}:nth-of-type(${same.indexOf(node) + 1})` : tag);
    }
    return bounded(parts.join(" > "), 512) || "body";
  }

  function ownLabel(element) {
    const title = [...element.children].find((child) => child.tagName.toLowerCase() === "title");
    return (
      element.getAttribute("aria-label") ||
      element.getAttribute("alt") ||
      element.getAttribute("title") ||
      title?.textContent ||
      ""
    );
  }

  // A part of a chart, such as one SVG shape, is named by the chart it belongs to.
  function describeElement(element) {
    let label = "";
    for (
      let node = element, depth = 0;
      node && node !== doc.body && depth < 4 && !label;
      node = node.parentElement, depth++
    )
      label = ownLabel(node);
    return {
      path: elementPath(element),
      label: bounded(label, 256),
      text: bounded(element.innerText ?? element.textContent, 512),
    };
  }

  const ignored = new Set(["SCRIPT", "STYLE", "HEAD", "META", "LINK", "TEMPLATE", "NOSCRIPT"]);

  // The first outermost elements lying mostly inside the box; when none does, the
  // smallest element that holds the whole box, with where the box lies within it.
  // One walk skips each chosen subtree and stops after a bounded number of elements,
  // so a huge or generated page cannot stall the selection.
  function coveredElements(box) {
    const inside = [];
    const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_ELEMENT);
    let element = walker.nextNode();
    let visited = 0;
    while (element && inside.length < 8 && visited++ < 5000) {
      let skip = ignored.has(element.tagName) || Boolean(element.dataset?.rcpSelection);
      if (!skip) {
        const rect = element.getBoundingClientRect();
        const area = rect.width * rect.height;
        const overlap =
          Math.max(0, Math.min(rect.right, box.right) - Math.max(rect.left, box.left)) *
          Math.max(0, Math.min(rect.bottom, box.bottom) - Math.max(rect.top, box.top));
        if (area && overlap / area >= 0.5) {
          inside.push(element);
          skip = true;
        }
      }
      if (skip) {
        let next = walker.nextSibling();
        while (!next && walker.parentNode()) next = walker.nextSibling();
        element = next;
      } else element = walker.nextNode();
    }
    if (inside.length) return inside.map(describeElement);
    let holder = doc.elementFromPoint((box.left + box.right) / 2, (box.top + box.bottom) / 2);
    while (holder && holder !== doc.body && holder !== doc.documentElement) {
      const rect = holder.getBoundingClientRect();
      if (
        rect.width &&
        rect.height &&
        rect.left <= box.left &&
        rect.top <= box.top &&
        rect.right >= box.right &&
        rect.bottom >= box.bottom
      ) {
        // Rounded down so a region never reads as reaching past its element's edge.
        const floor = (value) => Math.floor(Math.min(1, Math.max(0, value)) * 1e6) / 1e6;
        const x = floor((box.left - rect.left) / rect.width);
        const y = floor((box.top - rect.top) / rect.height);
        const width = floor(Math.min(1 - x, (box.right - box.left) / rect.width));
        const height = floor(Math.min(1 - y, (box.bottom - box.top) / rect.height));
        const described = describeElement(holder);
        return [width && height ? { ...described, region: { x, y, width, height } } : described];
      }
      holder = holder.parentElement;
    }
    return [];
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
        !isActive() ||
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
      // Preventing native selection also prevents focus entering an iframe.
      // Keep Escape with the active drag instead of racing a parent clear message.
      if (surface === doc) view.focus();
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
      const box = { left, top, right, bottom };
      publish({
        kind: "box",
        rect: {
          x: (left - area.left) / area.width,
          y: (top - area.top) / area.height,
          width: (right - left) / area.width,
          height: (bottom - top) / area.height,
        },
        viewport: { width: Math.round(area.width), height: Math.round(area.height) },
        elements: surface === doc ? coveredElements(box) : [],
      });
    },
    true,
  );
  if (surface === doc)
    listen(doc, "mouseup", (event) => {
      if (!event.isTrusted || !isActive()) return;
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
  function abortDrag(event) {
    if (!drag || (event?.pointerId !== undefined && event.pointerId !== drag.id)) return;
    if (drag.started) {
      clear();
      publish(null);
    } else endDrag();
  }
  listen(surface, "pointercancel", abortDrag, true);
  listen(capture, "lostpointercapture", abortDrag);
  listen(view, "blur", abortDrag);
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
    excerpt.textContent = selection ? describeSelection(selection) : "";
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

// One short line for a selection, shared by the viewer's comments and the chat draft.
function describeSelection(selection) {
  if (selection.kind === "whole") return "Whole artifact";
  if (selection.kind === "text") return `"${selection.text}"`;
  const elements = selection.elements;
  if (!elements) return selection.labels || "Boxed area";
  const [first, ...rest] = elements;
  if (!first) return `Boxed area ${describeRegion(selection.rect)}`;
  const name = first.label || first.text.slice(0, 80) || first.path;
  if (first.region) return `${name}, ${describeRegion(first.region)}`;
  return rest.length ? `${name} and ${rest.length} more` : name;
}

function describeRegion(rect) {
  const percent = (value) => `${Math.round(value * 100)}%`;
  return `x ${percent(rect.x)}–${percent(rect.x + rect.width)}, y ${percent(rect.y)}–${percent(rect.y + rect.height)}`;
}
