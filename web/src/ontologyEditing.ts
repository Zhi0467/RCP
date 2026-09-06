import type {
  BaseNodeType,
  GraphNode,
  GraphEditOptions,
  NewNode,
  OntologyFieldDefinition,
  OntologyRelationDefinition,
  OntologyState,
  OntologyTypeDefinition,
} from "./types";

export const baseOntologyTypes: Array<{
  name: BaseNodeType;
  label: string;
  layer: "epistemic" | "action";
  primaryField: string;
  primaryLabel: string;
}> = [
  {
    name: "research_question",
    label: "Research question",
    layer: "epistemic",
    primaryField: "question",
    primaryLabel: "Question",
  },
  {
    name: "hypothesis",
    label: "Hypothesis",
    layer: "epistemic",
    primaryField: "statement",
    primaryLabel: "Statement",
  },
  {
    name: "decision",
    label: "Decision",
    layer: "action",
    primaryField: "question",
    primaryLabel: "Question",
  },
  {
    name: "experiment",
    label: "Experiment",
    layer: "action",
    primaryField: "objective",
    primaryLabel: "Objective",
  },
  {
    name: "evidence",
    label: "Evidence",
    layer: "epistemic",
    primaryField: "observation",
    primaryLabel: "Observation",
  },
  {
    name: "blocker",
    label: "Blocker",
    layer: "action",
    primaryField: "description",
    primaryLabel: "Description",
  },
];

export const ontologyNamePattern = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/;

export function ontologyTypeNames(ontology: OntologyState): string[] {
  return [
    ...baseOntologyTypes.map((item) => item.name),
    ...ontology.types.map((item) => item.name),
  ];
}

export function activeCustomTypes(ontology: OntologyState): OntologyTypeDefinition[] {
  return ontology.types.filter((item) => !item.deprecated);
}

export function activeFieldsForNode(
  ontology: OntologyState,
  baseType: BaseNodeType,
  extensionType?: string | null,
): OntologyFieldDefinition[] {
  const owners = new Set([baseType, ...(extensionType ? [extensionType] : [])]);
  return ontology.fields.filter(
    (item) => owners.has(item.owner_type as BaseNodeType) && !item.deprecated,
  );
}

export function upsertOntologyType(
  ontology: OntologyState,
  definition: OntologyTypeDefinition,
  previousName?: string,
): OntologyState {
  const replaced = ontology.types.filter((item) => item.name !== (previousName ?? definition.name));
  const fields =
    previousName && previousName !== definition.name
      ? ontology.fields.map((item) =>
          item.owner_type === previousName ? { ...item, owner_type: definition.name } : item,
        )
      : ontology.fields;
  const relations =
    previousName && previousName !== definition.name
      ? ontology.relations.map((item) => ({
          ...item,
          source_types: item.source_types.map((name) =>
            name === previousName ? definition.name : name,
          ),
          target_types: item.target_types.map((name) =>
            name === previousName ? definition.name : name,
          ),
        }))
      : ontology.relations;
  return { types: [...replaced, definition], fields, relations };
}

export function canRemoveOntologyType(ontology: OntologyState, name: string): boolean {
  const type = ontology.types.find((item) => item.name === name);
  if (!type?.deprecated) return false;
  const ownedFieldsAreDeprecated = ontology.fields
    .filter((item) => item.owner_type === name)
    .every((item) => item.deprecated);
  const dependentRelationsAreDeprecated = ontology.relations
    .filter((item) => item.source_types.includes(name) || item.target_types.includes(name))
    .every((item) => item.deprecated);
  return ownedFieldsAreDeprecated && dependentRelationsAreDeprecated;
}

export function removeOntologyType(ontology: OntologyState, name: string): OntologyState {
  return {
    types: ontology.types.filter((item) => item.name !== name),
    fields: ontology.fields.filter((item) => item.owner_type !== name),
    relations: ontology.relations.filter(
      (item) => !item.source_types.includes(name) && !item.target_types.includes(name),
    ),
  };
}

export function upsertOntologyField(
  ontology: OntologyState,
  definition: OntologyFieldDefinition,
  previousKey?: string,
): OntologyState {
  const key = previousKey ?? `${definition.owner_type}.${definition.name}`;
  return {
    ...ontology,
    fields: [
      ...ontology.fields.filter((item) => `${item.owner_type}.${item.name}` !== key),
      definition,
    ],
  };
}

export function removeOntologyField(
  ontology: OntologyState,
  ownerType: string,
  name: string,
): OntologyState {
  return {
    ...ontology,
    fields: ontology.fields.filter((item) => item.owner_type !== ownerType || item.name !== name),
  };
}

export function upsertOntologyRelation(
  ontology: OntologyState,
  definition: OntologyRelationDefinition,
  previousName?: string,
): OntologyState {
  return {
    ...ontology,
    relations: [
      ...ontology.relations.filter((item) => item.name !== (previousName ?? definition.name)),
      definition,
    ],
  };
}

export function removeOntologyRelation(ontology: OntologyState, name: string): OntologyState {
  return { ...ontology, relations: ontology.relations.filter((item) => item.name !== name) };
}

export function makeHumanNode(
  definition: { name: string; base_type: BaseNodeType },
  prefixes: GraphEditOptions["node_prefixes"],
  slug: string,
  title: string,
  primaryText: string,
  origin: GraphNode["origin"],
  extensionFields: GraphNode["extension_fields"],
): NewNode {
  const base = baseOntologyTypes.find((item) => item.name === definition.base_type)!;
  return {
    id: humanNodeId(definition, prefixes, slug),
    type: base.name,
    extension_type: definition.name === base.name ? null : definition.name,
    extension_fields: extensionFields,
    title: title.trim(),
    [base.primaryField]: primaryText.trim(),
    ...(base.name === "evidence" && origin ? { origin } : {}),
  };
}

export function humanNodeId(
  definition: { name: string; base_type: BaseNodeType },
  prefixes: GraphEditOptions["node_prefixes"],
  slug: string,
): string {
  const prefix =
    definition.name === definition.base_type ? prefixes[definition.base_type] : definition.name;
  return `${prefix}/${normalizeSlug(slug)}`;
}

export function normalizeSlug(slug: string): string {
  return slug
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}
