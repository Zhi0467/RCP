import { Check } from "lucide-react";
import { useState } from "react";
import { activeFieldsForNode, baseOntologyTypes } from "./ontologyEditing";
import { carriesOrigin, primaryField } from "./researchType";
import type {
  ExtensionFieldValue,
  GraphNode,
  NewNode,
  OntologyFieldDefinition,
  OntologyState,
} from "../core/types";

/** Edits human-entered fields; preview metadata never returns to the draft. */
export function StagedNodeEditor({
  node,
  ontology,
  disabled,
  onSave,
  onCancel,
}: {
  node: NewNode;
  ontology: OntologyState;
  disabled: boolean;
  onSave: (node: NewNode) => void;
  onCancel: () => void;
}) {
  const primary = primaryField(node.type) as keyof NewNode;
  const base = baseOntologyTypes.find((item) => item.name === node.type)!;
  const [title, setTitle] = useState(node.title);
  const [text, setText] = useState(String(node[primary] ?? ""));
  const [origin, setOrigin] = useState(node.origin);
  const [values, setValues] = useState(node.extension_fields);
  const fields = activeFieldsForNode(ontology, node.type, node.extension_type);
  const valid = Boolean(
    title.trim() &&
    text.trim() &&
    (!carriesOrigin(node.type) || (origin && origin !== "unknown")) &&
    fields
      .filter((field) => field.required)
      .every((field) => hasFieldValue(values[field.name], field)),
  );
  return (
    <form
      className="node-edit-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (!valid || disabled) return;
        onSave({
          id: node.id,
          type: node.type,
          title: title.trim(),
          extension_type: node.extension_type,
          extension_fields: Object.fromEntries(
            Object.entries(values).filter(([name, value]) => {
              const field = fields.find((item) => item.name === name);
              return !field || hasFieldValue(value, field);
            }),
          ),
          [primary]: text.trim(),
          ...(carriesOrigin(node.type) ? { origin } : {}),
        });
      }}
    >
      <label className="node-edit-field">
        <span>Title</span>
        <input
          value={title}
          disabled={disabled}
          onChange={(event) => setTitle(event.target.value)}
        />
      </label>
      <label className="node-edit-field">
        <span>{base.primaryLabel}</span>
        <textarea
          aria-label={base.primaryLabel}
          rows={3}
          value={text}
          disabled={disabled}
          onChange={(event) => setText(event.target.value)}
        />
      </label>
      {carriesOrigin(node.type) && (
        <label className="node-edit-field">
          <span>Origin</span>
          <select
            aria-label="Origin"
            value={origin ?? "unknown"}
            disabled={disabled}
            onChange={(event) => setOrigin(event.target.value as GraphNode["origin"])}
          >
            <option value="unknown" disabled>
              Unknown
            </option>
            <option value="internal_run">Internal run</option>
            <option value="external_publication">External publication</option>
            <option value="external_instance">External instance</option>
            <option value="analytic">Analytic</option>
          </select>
        </label>
      )}
      {fields.map((field) => (
        <ExtensionFieldInput
          key={`${field.owner_type}.${field.name}`}
          field={field}
          value={values[field.name]}
          disabled={disabled}
          onChange={(value) => setValues((current) => ({ ...current, [field.name]: value }))}
        />
      ))}
      <div className="node-detail-actions">
        <button className="button ghost" type="button" onClick={onCancel}>
          Cancel
        </button>
        <button className="button primary compact" type="submit" disabled={disabled || !valid}>
          <Check size={14} /> Done
        </button>
      </div>
    </form>
  );
}

function ExtensionFieldInput({
  field,
  value,
  disabled,
  onChange,
}: {
  field: OntologyFieldDefinition;
  value: ExtensionFieldValue | undefined;
  disabled: boolean;
  onChange: (value: ExtensionFieldValue) => void;
}) {
  const label = `${typeLabel(field.name)}${field.required ? " *" : ""}`;
  if (field.kind === "boolean")
    return (
      <label className="node-edit-field">
        <span>{label}</span>
        <select
          value={value === true ? "true" : value === false ? "false" : ""}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value === "true")}
        >
          <option value="" disabled>
            —
          </option>
          <option value="true">Yes</option>
          <option value="false">No</option>
        </select>
      </label>
    );
  if (field.kind === "text_list")
    return (
      <label className="node-edit-field">
        <span>{label}</span>
        <textarea
          rows={3}
          value={Array.isArray(value) ? value.join("\n") : ""}
          disabled={disabled}
          onChange={(event) =>
            onChange(
              event.target.value
                .split(/\r?\n/)
                .map((item) => item.trim())
                .filter(Boolean),
            )
          }
        />
      </label>
    );
  return (
    <label className="node-edit-field">
      <span>{label}</span>
      <input
        type={field.kind === "number" ? "number" : "text"}
        value={typeof value === "string" || typeof value === "number" ? value : ""}
        disabled={disabled}
        onChange={(event) =>
          onChange(
            field.kind === "number" && event.target.value !== ""
              ? Number(event.target.value)
              : event.target.value,
          )
        }
      />
    </label>
  );
}

function hasFieldValue(
  value: ExtensionFieldValue | undefined,
  field: OntologyFieldDefinition,
): boolean {
  if (field.kind === "boolean") return typeof value === "boolean";
  if (field.kind === "number") return typeof value === "number" && Number.isFinite(value);
  if (field.kind === "text_list") return Array.isArray(value) && value.length > 0;
  return typeof value === "string" && value.trim().length > 0;
}

function typeLabel(value: string): string {
  return value.replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase());
}
