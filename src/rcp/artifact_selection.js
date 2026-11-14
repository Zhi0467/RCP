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
  let hover = null;
  let holdTimer = null;
  let touchArmed = false;
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
    clearTimeout(holdTimer);
    touchArmed = false;
    const id = drag?.id;
    drag = null;
    if (id !== undefined && capture.hasPointerCapture(id)) capture.releasePointerCapture(id);
  }

  function clear() {
    hover?.remove();
    hover = null;
    mark?.remove();
    mark = null;
    anchor = null;
    endDrag();
  }

  // The reader dismissed the offer: its mark and its text go, so a drag over text
  // still highlighted cannot move that text instead of selecting.
  function dismiss() {
    clear();
    doc.getSelection()?.removeAllRanges();
    publish(null);
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
      if (!event.isTrusted || !isActive() || event.button !== 0 || !event.isPrimary) return;
      endDrag();
      areaGesture = false;
      if (
        event.target.closest(
          'a,button,input,textarea,select,summary,[contenteditable],[draggable="true"]',
        ) ||
        (event.pointerType !== "touch" && textAtPoint(event))
      )
        return;
      if (surface === doc) view.focus();
      areaGesture = true;
      drag = {
        id: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        started: false,
        element: event.target,
        touch: event.pointerType === "touch",
      };
      if (drag.touch)
        holdTimer = setTimeout(() => {
          touchArmed = true;
        }, 450);
      capture.setPointerCapture(event.pointerId);
    },
    true,
  );

  listen(
    surface,
    "pointermove",
    (event) => {
      if (!event.isTrusted || !drag || event.pointerId !== drag.id) return;
      if (drag.touch && !touchArmed) {
        if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) > 8) endDrag();
        return;
      }
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
      if (!drawn) {
        if (surface === doc) {
          if (!publishText()) pickElement(element);
        } else dismiss();
        return;
      }
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
      if (!publishText()) pickElement(event.target);
    });
  function publishText() {
    const selection = doc.getSelection();
    const text = bounded(selection?.toString(), 4096);
    if (!text || !selection?.rangeCount) return false;
    clear();
    const range = selection.getRangeAt(0);
    const container =
      range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
        ? range.commonAncestorContainer
        : range.commonAncestorContainer.parentElement;
    publish({ kind: "text", text, surrounding_text: bounded(container?.textContent, 6144) });
    return true;
  }
  function pickElement(element) {
    if (!(element instanceof Element) || element.closest("[data-rcp-selection]")) return;
    clear();
    publish({ kind: "element", ...describeElement(element) });
  }
  if (surface === doc)
    listen(surface, "pointermove", (event) => {
      if (!event.isTrusted || !isActive() || drag || event.pointerType === "touch") return;
      const element = event.target;
      if (!(element instanceof Element) || element.closest("[data-rcp-selection]")) return;
      hover?.remove();
      hover = doc.createElement("div");
      hover.dataset.rcpSelection = "hover";
      const rect = element.getBoundingClientRect();
      Object.assign(hover.style, {
        position: "fixed",
        pointerEvents: "none",
        zIndex: "2147483646",
        left: `${rect.left}px`,
        top: `${rect.top}px`,
        width: `${rect.width}px`,
        height: `${rect.height}px`,
        border: "1px solid #bd5b36",
      });
      const chip = doc.createElement("span");
      chip.textContent = ownLabel(element) || element.tagName.toLowerCase();
      Object.assign(chip.style, {
        position: "absolute",
        left: "0",
        top: "0",
        background: "#bd5b36",
        color: "white",
        font: "12px sans-serif",
        padding: "2px 4px",
      });
      hover.appendChild(chip);
      doc.documentElement.appendChild(hover);
    });
  listen(
    surface,
    "touchmove",
    (event) => {
      if (touchArmed) event.preventDefault();
    },
    { passive: false },
  );
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
    if (event.key === "Escape") dismiss();
  });
  return clear;
}

// One short line for a selection, shared by the viewer's comments and the chat draft.
function describeSelection(selection) {
  if (selection.kind === "whole") return "Whole artifact";
  if (selection.kind === "text") return `"${selection.text}"`;
  if (selection.kind === "element") return selection.label || selection.text || selection.path;
  if (selection.kind === "pdf") return `Page ${selection.page}: ${selection.text || ""}`;
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

function installArtifactLocator(doc, publish) {
  const view = doc.defaultView;
  const tracked = new Map();
  let scheduled = false;
  let flash = null;
  let flashTimer;
  function textRange(anchor) {
    if (typeof anchor.text !== "string" || !anchor.text || anchor.text.length > 6144) return null;
    const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        return node.nodeType === Node.ELEMENT_NODE &&
          node.matches("script,style,noscript,[data-rcp-selection]")
          ? NodeFilter.FILTER_REJECT
          : NodeFilter.FILTER_ACCEPT;
      },
    });
    const blocks = new Map();
    const blockOf = (node) => {
      const parent = node.parentElement;
      if (!parent || parent === doc.body) return doc.body;
      if (!blocks.has(parent)) {
        const display = view.getComputedStyle(parent).display;
        blocks.set(
          parent,
          display.startsWith("inline") || display === "contents" ? blockOf(parent) : parent,
        );
      }
      return blocks.get(parent);
    };
    let text = "";
    const offsets = [];
    let previousBlock;
    let breakBefore = false;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.nodeType === Node.ELEMENT_NODE) {
        if (node.tagName === "BR") breakBefore = true;
        continue;
      }
      const value = node.textContent;
      if (!value) continue;
      const block = blockOf(node);
      if (
        text &&
        !text.endsWith(" ") &&
        (breakBefore || (previousBlock && block !== previousBlock))
      ) {
        text += " ";
        offsets.push({ node, offset: 0 });
      }
      previousBlock = block;
      breakBefore = false;
      for (let i = 0; i < value.length; i++) {
        const character = /\s/.test(value[i]) ? " " : value[i];
        if (character === " " && text.endsWith(" ")) continue;
        text += character;
        offsets.push({ node, offset: i });
      }
    }
    const normalize = (value) =>
      String(value || "")
        .replace(/\s+/g, " ")
        .trim();
    const quote = normalize(anchor.text);
    const surrounding = normalize(anchor.surrounding_text);
    const contextAt = surrounding ? text.indexOf(surrounding) : -1;
    const quoteAt = surrounding.indexOf(quote);
    const start = contextAt >= 0 && quoteAt >= 0 ? contextAt + quoteAt : text.indexOf(quote);
    if (start < 0) return null;
    const first = offsets[start];
    const last = offsets[start + quote.length - 1];
    if (!first || !last) return null;
    const range = doc.createRange();
    range.setStart(first.node, first.offset);
    range.setEnd(last.node, last.offset + 1);
    return { rect: range.getBoundingClientRect(), element: first.node.parentElement };
  }
  function find(anchor) {
    if (anchor.kind === "text") return textRange(anchor);
    const descriptor = anchor.kind === "box" ? anchor.elements?.[0] : anchor;
    if (anchor.kind !== "whole" && typeof descriptor?.path !== "string") return null;
    if (descriptor?.path?.length > 512) return null;
    let element;
    try {
      element = anchor.kind === "whole" ? doc.body : doc.querySelector(descriptor.path);
    } catch {
      return null;
    }
    if (!element) return null;
    const bounds = element.getBoundingClientRect();
    const region = descriptor?.region;
    if (region) {
      if (
        ![region.x, region.y, region.width, region.height].every(
          (value) => Number.isFinite(value) && value >= 0 && value <= 1,
        )
      )
        return null;
      return {
        element,
        rect: {
          x: bounds.x + bounds.width * region.x,
          y: bounds.y + bounds.height * region.y,
          width: bounds.width * region.width,
          height: bounds.height * region.height,
        },
      };
    }
    return { element, rect: bounds };
  }
  function report(id, anchor) {
    const found = find(anchor);
    const bounds = found?.rect;
    const rect =
      bounds && bounds.width > 0 && bounds.height > 0
        ? { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height }
        : null;
    publish({ kind: "rcp-artifact-position", id, rect });
    return found;
  }
  function refresh() {
    if (scheduled) return;
    scheduled = true;
    view.requestAnimationFrame(() => {
      scheduled = false;
      for (const [id, anchor] of tracked) report(id, anchor);
    });
  }
  view.addEventListener("scroll", refresh, true);
  view.addEventListener("resize", refresh);
  const observer = new ResizeObserver(refresh);
  observer.observe(doc.documentElement);
  return (request) => {
    if (!request || typeof request.id !== "string" || request.id.length > 128) return;
    const { id, anchor } = request;
    if (anchor === null) {
      tracked.delete(id);
      return;
    }
    if (!anchor || !["whole", "text", "element", "box"].includes(anchor.kind)) return;
    tracked.set(id, anchor);
    const found = report(id, anchor);
    if (!request.scroll || !found) return;
    for (
      let element = found.element;
      element && element !== doc.body && element !== doc.documentElement;
      element = element.parentElement
    ) {
      const style = view.getComputedStyle(element);
      const current = find(anchor);
      if (!current) break;
      const rect = current.rect;
      const bounds = element.getBoundingClientRect();
      const left = bounds.left + element.clientLeft;
      const top = bounds.top + element.clientTop;
      const dx =
        /auto|scroll|hidden/.test(style.overflowX) &&
        (rect.x < left || rect.x + rect.width > left + element.clientWidth)
          ? rect.x + rect.width / 2 - left - element.clientWidth / 2
          : 0;
      const dy =
        /auto|scroll|hidden/.test(style.overflowY) &&
        (rect.y < top || rect.y + rect.height > top + element.clientHeight)
          ? rect.y + rect.height / 2 - top - element.clientHeight / 2
          : 0;
      if (dx || dy) element.scrollBy({ left: dx, top: dy, behavior: "instant" });
    }
    const target = find(anchor)?.rect;
    if (target)
      view.scrollBy({
        left:
          target.x < 0 || target.x + target.width > view.innerWidth
            ? target.x + target.width / 2 - view.innerWidth / 2
            : 0,
        top:
          target.y < 0 || target.y + target.height > view.innerHeight
            ? target.y + target.height / 2 - view.innerHeight / 2
            : 0,
        behavior: "instant",
      });
    const current = report(id, anchor);
    if (!current) return;
    flash?.remove();
    clearTimeout(flashTimer);
    flash = doc.createElement("div");
    flash.dataset.rcpSelection = "locate";
    const rect = current.rect;
    Object.assign(flash.style, {
      position: "fixed",
      pointerEvents: "none",
      zIndex: "2147483647",
      left: `${rect.x}px`,
      top: `${rect.y}px`,
      width: `${rect.width}px`,
      height: `${rect.height}px`,
      background: "rgba(189,91,54,.2)",
      outline: "2px solid #bd5b36",
    });
    doc.documentElement.appendChild(flash);
    flashTimer = setTimeout(() => {
      flash?.remove();
      flash = null;
    }, 1100);
  };
}
