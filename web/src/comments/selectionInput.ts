import type { CommentAnchor, CommentElement, CommentRect } from "./types.ts";

function record(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function bounded(value: unknown, max: number): value is string {
  return typeof value === "string" && new TextEncoder().encode(value).length <= max;
}

function rect(value: unknown): value is CommentRect {
  if (!record(value)) return false;
  const { x, y, width, height } = value;
  return (
    [x, y, width, height].every((n) => typeof n === "number" && Number.isFinite(n)) &&
    typeof x === "number" &&
    typeof y === "number" &&
    typeof width === "number" &&
    typeof height === "number" &&
    x >= 0 &&
    y >= 0 &&
    width > 0 &&
    height > 0 &&
    x + width <= 1.000001 &&
    y + height <= 1.000001
  );
}

function element(value: unknown): CommentElement | null {
  if (
    !record(value) ||
    !bounded(value.path, 512) ||
    !value.path ||
    !bounded(value.label ?? "", 256) ||
    !bounded(value.text ?? "", 512)
  )
    return null;
  if (value.region != null && !rect(value.region)) return null;
  return {
    path: value.path,
    label: String(value.label ?? ""),
    text: String(value.text ?? ""),
    ...(rect(value.region) ? { region: value.region } : {}),
  };
}

export function selectionInput(value: unknown): CommentAnchor | null {
  if (!record(value)) return null;
  const base = { comment: "", ...(bounded(value.where, 256) ? { where: value.where } : {}) };
  switch (value.kind) {
    case "whole":
      return { kind: "whole", ...base };
    case "text":
      return bounded(value.text, 4096) && bounded(value.surrounding_text ?? "", 6144)
        ? {
            kind: "text",
            text: value.text,
            surrounding_text: String(value.surrounding_text ?? ""),
            ...base,
          }
        : null;
    case "element": {
      const picked = element(value);
      return picked
        ? { kind: "element", path: picked.path, label: picked.label, text: picked.text, ...base }
        : null;
    }
    case "pdf":
      return Number.isInteger(value.page) &&
        typeof value.page === "number" &&
        value.page > 0 &&
        rect(value.rect) &&
        bounded(value.text ?? "", 4096)
        ? {
            kind: "pdf",
            page: value.page,
            rect: value.rect,
            text: String(value.text ?? ""),
            ...base,
          }
        : null;
    case "box": {
      if (
        !rect(value.rect) ||
        !record(value.viewport) ||
        !Array.isArray(value.elements) ||
        value.elements.length > 8
      )
        return null;
      const { width, height } = value.viewport;
      if (
        typeof width !== "number" ||
        typeof height !== "number" ||
        !Number.isInteger(width) ||
        !Number.isInteger(height) ||
        width < 1 ||
        height < 1 ||
        width > 32768 ||
        height > 32768
      )
        return null;
      const elements = value.elements.map(element);
      if (elements.some((item) => item === null)) return null;
      return {
        kind: "box",
        rect: value.rect,
        viewport: { width, height },
        elements: elements.filter((item): item is CommentElement => item !== null),
        ...base,
      };
    }
    default:
      return null;
  }
}
