import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import {
  baseOntologyTypes,
  canRemoveOntologyType,
  ontologyNamePattern,
  ontologyTypeNames,
  removeOntologyField,
  removeOntologyRelation,
  removeOntologyType,
  upsertOntologyField,
  upsertOntologyRelation,
  upsertOntologyType,
} from "../ontologyEditing";
import type {
  BaseNodeType,
  OntologyFieldDefinition,
  OntologyFieldKind,
  OntologyLayer,
  OntologyRelationDefinition,
  OntologyState,
  OntologyTypeDefinition,
} from "../types";

interface Props {
  ontology: OntologyState;
  canonicalOntology: OntologyState;
  disabled?: boolean;
  staged?: boolean;
  onChange: (ontology: OntologyState) => void;
}

const blankType: OntologyTypeDefinition = {
  name: "",
  definition: "",
  base_type: "hypothesis",
  layer: "epistemic",
  deprecated: false,
};
const blankField: OntologyFieldDefinition = {
  owner_type: "hypothesis",
  name: "",
  definition: "",
  kind: "text",
  required: false,
  agent_writable: true,
  deprecated: false,
};
const blankRelation: OntologyRelationDefinition = {
  name: "",
  definition: "",
  source_types: ["hypothesis"],
  target_types: ["evidence"],
  layer: "epistemic",
  deprecated: false,
};

export function OntologyEditor({ ontology, canonicalOntology, disabled = false, staged = false, onChange }: Props) {
  const [newType, setNewType] = useState(blankType);
  const [newField, setNewField] = useState(blankField);
  const [newRelation, setNewRelation] = useState(blankRelation);
  const typeNames = ontologyTypeNames(ontology);
  const typeNameSet = new Set(typeNames);

  const addType = () => {
    if (!ontologyNamePattern.test(newType.name) || !newType.definition.trim() || typeNameSet.has(newType.name)) return;
    onChange(upsertOntologyType(ontology, { ...newType, definition: newType.definition.trim() }));
    setNewType(blankType);
  };
  const addField = () => {
    const duplicate = ontology.fields.some((item) => item.owner_type === newField.owner_type && item.name === newField.name);
    if (!ontologyNamePattern.test(newField.name) || !newField.definition.trim() || duplicate) return;
    onChange(upsertOntologyField(ontology, { ...newField, definition: newField.definition.trim() }));
    setNewField({ ...blankField, owner_type: newField.owner_type });
  };
  const addRelation = () => {
    const duplicate = ontology.relations.some((item) => item.name === newRelation.name);
    if (!ontologyNamePattern.test(newRelation.name) || !newRelation.definition.trim() || duplicate
      || newRelation.source_types.length === 0 || newRelation.target_types.length === 0) return;
    onChange(upsertOntologyRelation(ontology, { ...newRelation, definition: newRelation.definition.trim() }));
    setNewRelation(blankRelation);
  };

  return (
    <section className="settings-section ontology-settings">
      <header><h2>Ontology</h2><span className={staged ? "ontology-sync-state staged" : "ontology-sync-state"}>Project Sync</span></header>

      <div className="ontology-block">
        <h3>Base types</h3>
        <div className="ontology-base-grid">
          {baseOntologyTypes.map((item) => (
            <article key={item.name}><strong>{item.label}</strong><span>{item.layer}</span></article>
          ))}
        </div>
      </div>

      <div className="ontology-block">
        <h3>Custom types</h3>
        <div className="ontology-column-labels ontology-type-row"><span>Name</span><span>Definition</span><span>Base mapping</span><span>Layer</span><span>State</span><span /></div>
        <div className="ontology-definition-list">
          {ontology.types.map((item) => (
            <TypeRow
              key={item.name}
              item={item}
              removable={canRemoveOntologyType(canonicalOntology, item.name)}
              disabled={disabled}
              onChange={(next) => onChange(upsertOntologyType(ontology, next, item.name))}
              onRemove={() => onChange(removeOntologyType(ontology, item.name))}
            />
          ))}
        </div>
        <div className="ontology-add-row ontology-type-row">
          <input aria-label="New custom type name" pattern={ontologyNamePattern.source} value={newType.name} disabled={disabled} onChange={(event) => setNewType({ ...newType, name: event.target.value })} />
          <input aria-label="New custom type definition" value={newType.definition} disabled={disabled} onChange={(event) => setNewType({ ...newType, definition: event.target.value })} />
          <select aria-label="New custom type base mapping" value={newType.base_type} disabled={disabled} onChange={(event) => {
            const base_type = event.target.value as BaseNodeType;
            setNewType({ ...newType, base_type, layer: baseLayer(base_type) });
          }}>
            {baseOntologyTypes.map((item) => <option value={item.name} key={item.name}>{item.label}</option>)}
          </select>
          <span className="ontology-layer">{newType.layer}</span>
          <button className="icon-button" type="button" aria-label="Add custom type" disabled={disabled || !ontologyNamePattern.test(newType.name) || !newType.definition.trim() || typeNameSet.has(newType.name)} onClick={addType}><Plus size={14} /></button>
        </div>
      </div>

      <div className="ontology-block">
        <h3>Fields</h3>
        <div className="ontology-column-labels ontology-field-row"><span>Owner and name</span><span>Definition</span><span>Kind</span><span>Required</span><span>Agent</span><span>State</span><span /></div>
        <div className="ontology-definition-list">
          {ontology.fields.map((item) => (
            <FieldRow
              key={`${item.owner_type}.${item.name}`}
              item={item}
              removable={item.deprecated && Boolean(canonicalOntology.fields.find((candidate) => candidate.owner_type === item.owner_type && candidate.name === item.name)?.deprecated)}
              disabled={disabled}
              onChange={(next) => onChange(upsertOntologyField(ontology, next, `${item.owner_type}.${item.name}`))}
              onRemove={() => onChange(removeOntologyField(ontology, item.owner_type, item.name))}
            />
          ))}
        </div>
        <div className="ontology-add-row ontology-field-row">
          <select aria-label="New field owner type" value={newField.owner_type} disabled={disabled} onChange={(event) => setNewField({ ...newField, owner_type: event.target.value })}>
            {typeNames.map((name) => <option value={name} key={name}>{typeLabel(name)}</option>)}
          </select>
          <input aria-label="New field name" pattern={ontologyNamePattern.source} value={newField.name} disabled={disabled} onChange={(event) => setNewField({ ...newField, name: event.target.value })} />
          <input aria-label="New field definition" value={newField.definition} disabled={disabled} onChange={(event) => setNewField({ ...newField, definition: event.target.value })} />
          <FieldKindSelect value={newField.kind} disabled={disabled} label="New field kind" onChange={(kind) => setNewField({ ...newField, kind })} />
          <CheckField label="Required" checked={newField.required} disabled={disabled} onChange={(required) => setNewField({ ...newField, required })} />
          <CheckField label="Agent writable" checked={newField.agent_writable} disabled={disabled} onChange={(agent_writable) => setNewField({ ...newField, agent_writable })} />
          <button className="icon-button" type="button" aria-label="Add field" disabled={disabled || !ontologyNamePattern.test(newField.name) || !newField.definition.trim()} onClick={addField}><Plus size={14} /></button>
        </div>
      </div>

      <div className="ontology-block">
        <h3>Relations</h3>
        <div className="ontology-column-labels ontology-relation-labels"><span>Name</span><span>Definition</span><span>Layer</span><span>State</span><span /></div>
        <div className="ontology-definition-list">
          {ontology.relations.map((item) => (
            <RelationRow
              key={item.name}
              item={item}
              removable={item.deprecated && Boolean(canonicalOntology.relations.find((candidate) => candidate.name === item.name)?.deprecated)}
              typeNames={typeNames}
              disabled={disabled}
              onChange={(next) => onChange(upsertOntologyRelation(ontology, next, item.name))}
              onRemove={() => onChange(removeOntologyRelation(ontology, item.name))}
            />
          ))}
        </div>
        <div className="ontology-add-relation">
          <div className="ontology-add-row">
            <input aria-label="New relation name" pattern={ontologyNamePattern.source} value={newRelation.name} disabled={disabled} onChange={(event) => setNewRelation({ ...newRelation, name: event.target.value })} />
            <input aria-label="New relation definition" value={newRelation.definition} disabled={disabled} onChange={(event) => setNewRelation({ ...newRelation, definition: event.target.value })} />
            <LayerSelect value={newRelation.layer} disabled={disabled} label="New relation layer" onChange={(layer) => setNewRelation({ ...newRelation, layer })} />
            <button className="icon-button" type="button" aria-label="Add relation" disabled={disabled || !ontologyNamePattern.test(newRelation.name) || !newRelation.definition.trim()} onClick={addRelation}><Plus size={14} /></button>
          </div>
          <TypeSetEditor label="Source types" typeNames={typeNames} values={newRelation.source_types} disabled={disabled} onChange={(source_types) => setNewRelation({ ...newRelation, source_types })} />
          <TypeSetEditor label="Target types" typeNames={typeNames} values={newRelation.target_types} disabled={disabled} onChange={(target_types) => setNewRelation({ ...newRelation, target_types })} />
        </div>
      </div>
    </section>
  );
}

function TypeRow({ item, removable, disabled, onChange, onRemove }: { item: OntologyTypeDefinition; removable: boolean; disabled: boolean; onChange: (item: OntologyTypeDefinition) => void; onRemove: () => void }) {
  return <div className={`ontology-definition ontology-type-row${item.deprecated ? " deprecated" : ""}`}>
    <strong className="mono">{item.name}</strong>
    <input aria-label={`${item.name} definition`} value={item.definition} disabled={disabled} onChange={(event) => onChange({ ...item, definition: event.target.value })} />
    <select aria-label={`${item.name} base mapping`} value={item.base_type} disabled={disabled} onChange={(event) => {
      const base_type = event.target.value as BaseNodeType;
      onChange({ ...item, base_type, layer: baseLayer(base_type) });
    }}>{baseOntologyTypes.map((base) => <option value={base.name} key={base.name}>{base.label}</option>)}</select>
    <span className="ontology-layer">{item.layer}</span>
    <CheckField label="Deprecated" checked={item.deprecated} disabled={disabled} onChange={(deprecated) => onChange({ ...item, deprecated })} />
    {removable && <button className="icon-button danger" type="button" aria-label={`Remove ${item.name}`} disabled={disabled} onClick={onRemove}><Trash2 size={13} /></button>}
  </div>;
}

function FieldRow({ item, removable, disabled, onChange, onRemove }: { item: OntologyFieldDefinition; removable: boolean; disabled: boolean; onChange: (item: OntologyFieldDefinition) => void; onRemove: () => void }) {
  return <div className={`ontology-definition ontology-field-row${item.deprecated ? " deprecated" : ""}`}>
    <strong className="mono">{item.owner_type}.{item.name}</strong>
    <input aria-label={`${item.owner_type}.${item.name} definition`} value={item.definition} disabled={disabled} onChange={(event) => onChange({ ...item, definition: event.target.value })} />
    <FieldKindSelect value={item.kind} disabled={disabled} label={`${item.owner_type}.${item.name} kind`} onChange={(kind) => onChange({ ...item, kind })} />
    <CheckField label="Required" checked={item.required} disabled={disabled} onChange={(required) => onChange({ ...item, required })} />
    <CheckField label="Agent writable" checked={item.agent_writable} disabled={disabled} onChange={(agent_writable) => onChange({ ...item, agent_writable })} />
    <CheckField label="Deprecated" checked={item.deprecated} disabled={disabled} onChange={(deprecated) => onChange({ ...item, deprecated })} />
    {removable && <button className="icon-button danger" type="button" aria-label={`Remove ${item.owner_type}.${item.name}`} disabled={disabled} onClick={onRemove}><Trash2 size={13} /></button>}
  </div>;
}

function RelationRow({ item, removable, typeNames, disabled, onChange, onRemove }: { item: OntologyRelationDefinition; removable: boolean; typeNames: string[]; disabled: boolean; onChange: (item: OntologyRelationDefinition) => void; onRemove: () => void }) {
  return <div className={`ontology-definition ontology-relation-row${item.deprecated ? " deprecated" : ""}`}>
    <div className="ontology-add-row">
      <strong className="mono">{item.name}</strong>
      <input aria-label={`${item.name} definition`} value={item.definition} disabled={disabled} onChange={(event) => onChange({ ...item, definition: event.target.value })} />
      <LayerSelect value={item.layer} disabled={disabled} label={`${item.name} layer`} onChange={(layer) => onChange({ ...item, layer })} />
      <CheckField label="Deprecated" checked={item.deprecated} disabled={disabled} onChange={(deprecated) => onChange({ ...item, deprecated })} />
      {removable && <button className="icon-button danger" type="button" aria-label={`Remove ${item.name}`} disabled={disabled} onClick={onRemove}><Trash2 size={13} /></button>}
    </div>
    <TypeSetEditor label="Source types" typeNames={typeNames} values={item.source_types} disabled={disabled} onChange={(source_types) => source_types.length && onChange({ ...item, source_types })} />
    <TypeSetEditor label="Target types" typeNames={typeNames} values={item.target_types} disabled={disabled} onChange={(target_types) => target_types.length && onChange({ ...item, target_types })} />
  </div>;
}

function LayerSelect({ value, disabled, label, onChange }: { value: OntologyLayer; disabled: boolean; label: string; onChange: (value: OntologyLayer) => void }) {
  return <select aria-label={label} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value as OntologyLayer)}><option value="epistemic">Epistemic</option><option value="action">Action</option></select>;
}

function FieldKindSelect({ value, disabled, label, onChange }: { value: OntologyFieldKind; disabled: boolean; label: string; onChange: (value: OntologyFieldKind) => void }) {
  return <select aria-label={label} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value as OntologyFieldKind)}><option value="text">Text</option><option value="number">Number</option><option value="boolean">Boolean</option><option value="text_list">Text list</option></select>;
}

function CheckField({ label, checked, disabled, onChange }: { label: string; checked: boolean; disabled: boolean; onChange: (checked: boolean) => void }) {
  return <label className="ontology-check"><input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} /><span>{label}</span></label>;
}

function TypeSetEditor({ label, typeNames, values, disabled, onChange }: { label: string; typeNames: string[]; values: string[]; disabled: boolean; onChange: (values: string[]) => void }) {
  return <fieldset className="ontology-type-set"><legend>{label}</legend>{typeNames.map((name) => <label key={name}><input type="checkbox" checked={values.includes(name)} disabled={disabled} onChange={(event) => onChange(event.target.checked ? [...values, name] : values.filter((value) => value !== name))} /><span>{typeLabel(name)}</span></label>)}</fieldset>;
}

function typeLabel(name: string): string {
  return name.replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase());
}

function baseLayer(type: BaseNodeType): OntologyLayer {
  return baseOntologyTypes.find((item) => item.name === type)!.layer;
}
