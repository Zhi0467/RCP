import {
  CHOOSER_CHOICE_FIELDS,
  CONTEXT_FIELD_ORDER,
  FIELD_LABELS,
  isChooser,
  primaryField,
} from "./researchType.ts";
import type { GraphNode } from "./types";

/** Reader-facing field labels; the research layer owns the table. */
export const humanFieldLabels: Record<string, string> = FIELD_LABELS;

export function presentNode(node: GraphNode) {
  const key = primaryField(node.type);
  const value = readableValue(node[key]) ? node[key] : node.title;
  const context = CONTEXT_FIELD_ORDER.flatMap((field) => {
    if (isChooser(node.type) && CHOOSER_CHOICE_FIELDS.includes(field)) {
      return [];
    }
    const raw = field === "proxies" ? proxyLines(node.proxies) : node[field];
    return readableValue(raw)
      ? [{ key: field, label: humanFieldLabels[field] ?? humanize(field), value: raw }]
      : [];
  });
  return { key, label: humanFieldLabels[key], value, context };
}

function proxyLines(proxies: GraphNode["proxies"]): string[] {
  return (proxies ?? []).map((proxy) => `${proxy.stands_for}, measured as ${proxy.measure}`);
}

export function nodeTypeLabel(node: GraphNode): string {
  return humanize(node.extension_type ?? node.type);
}

export function humanize(key: string): string {
  return key.replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase());
}

function readableValue(value: unknown): boolean {
  return (
    value !== null &&
    value !== undefined &&
    value !== "" &&
    (!Array.isArray(value) || value.length > 0)
  );
}
