import type {
  ExperimentProxy,
  ExtensionFieldValue,
  GraphNode,
  OntologyFieldDefinition,
  OntologyState,
} from "../core/types";
import { editFieldsFor } from "./researchType.ts";

export interface NodeEditField {
  key: string;
  label: string;
  kind: "text" | "multiline" | "list" | "proxies" | "number" | "boolean" | "select";
  options?: { value: string; label: string }[];
  nullable?: boolean;
  min?: number;
  integer?: boolean;
  extensionName?: string;
}

export function editableNodeFields(node: GraphNode, ontology?: OntologyState): NodeEditField[] {
  const ownerTypes = new Set([node.type, ...(node.extension_type ? [node.extension_type] : [])]);
  const baseFields = editFieldsFor(node);
  const extensionFields = ontology
    ? ontology.fields
        .filter((field) => ownerTypes.has(field.owner_type) && !field.deprecated)
        .map(toEditField)
    : [];
  return [...baseFields, ...extensionFields];
}

export function nodeEditDraft(node: GraphNode, ontology?: OntologyState): Record<string, string> {
  return Object.fromEntries(
    editableNodeFields(node, ontology).map((field) => [
      field.key,
      draftValue(
        field,
        field.extensionName ? node.extension_fields[field.extensionName] : node[field.key],
      ),
    ]),
  );
}

export function changedNodeFields(
  node: GraphNode,
  draft: Record<string, string>,
  ontology?: OntologyState,
): Record<string, FieldValue | Record<string, string | number | boolean | string[]>> {
  const fields = editableNodeFields(node, ontology);
  const baseChanges = Object.fromEntries(
    fields
      .filter((field) => !field.extensionName)
      .flatMap((field) => {
        const next = normalizeField(field, draft[field.key] ?? "");
        const current = normalizeCurrent(field, node[field.key]);
        return equalValues(current, next) ? [] : [[field.key, next]];
      }),
  );
  const extensionDefinitions = fields.filter((field) => field.extensionName);
  const extensionChanged = extensionDefinitions.some((field) => {
    const next = normalizeField(field, draft[field.key] ?? "");
    return !equalValues(normalizeCurrent(field, node.extension_fields[field.extensionName!]), next);
  });
  if (!extensionChanged) return baseChanges;
  const extension_fields = { ...node.extension_fields };
  for (const field of extensionDefinitions) {
    const value = normalizeField(field, draft[field.key] ?? "");
    if (value === null || value === "" || (Array.isArray(value) && value.length === 0)) {
      delete extension_fields[field.extensionName!];
    } else {
      // Extension fields are never proxy lists; their kinds come from the ontology.
      extension_fields[field.extensionName!] = value as ExtensionFieldValue;
    }
  }
  return { ...baseChanges, extension_fields };
}

type FieldValue = string | number | boolean | string[] | ExperimentProxy[] | null;

/** The draft form of a proxy list: one row per proxy, blank rows allowed while editing. */
export function proxyDraftRows(value: string): ExperimentProxy[] {
  try {
    const parsed: unknown = JSON.parse(value || "[]");
    return Array.isArray(parsed)
      ? parsed.map((row) => ({
          stands_for: typeof row?.stands_for === "string" ? row.stands_for : "",
          measure: typeof row?.measure === "string" ? row.measure : "",
        }))
      : [];
  } catch {
    return [];
  }
}

function proxyValue(rows: ExperimentProxy[]): ExperimentProxy[] {
  return rows
    .map((row) => ({ stands_for: row.stands_for.trim(), measure: row.measure.trim() }))
    .filter((row) => row.stands_for || row.measure);
}

function normalizeCurrent(field: NodeEditField, value: unknown): FieldValue {
  if (field.kind === "list")
    return arrayValue(value)
      .map((item) => item.trim())
      .filter(Boolean);
  if (field.kind === "proxies") return proxyValue(proxyDraftRows(JSON.stringify(value ?? [])));
  if (field.kind === "number") return typeof value === "number" ? value : null;
  if (field.kind === "boolean") return typeof value === "boolean" ? value : null;
  return normalizeField(field, stringValue(value));
}

function normalizeField(field: NodeEditField, value: string): FieldValue {
  if (field.kind === "list") {
    return value
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  if (field.kind === "proxies") return proxyValue(proxyDraftRows(value));
  if (field.nullable && value.trim() === "") return null;
  if (field.kind === "number") return Number(value);
  if (field.kind === "boolean") return value === "true";
  const normalized = value.trim();
  return field.nullable && normalized === "" ? null : normalized;
}

function arrayValue(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function stringValue(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

function equalValues(left: FieldValue, right: FieldValue): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function draftValue(field: NodeEditField, value: unknown): string {
  if (field.kind === "list") return arrayValue(value).join("\n");
  if (field.kind === "proxies") return JSON.stringify(proxyDraftRows(JSON.stringify(value ?? [])));
  if (field.kind === "boolean") return typeof value === "boolean" ? String(value) : "";
  return stringValue(value);
}

function toEditField(field: OntologyFieldDefinition): NodeEditField {
  return {
    key: `extension_fields.${field.name}`,
    label: field.name.replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase()),
    kind: field.kind === "text_list" ? "list" : field.kind === "text" ? "multiline" : field.kind,
    nullable: !field.required,
    extensionName: field.name,
  };
}
