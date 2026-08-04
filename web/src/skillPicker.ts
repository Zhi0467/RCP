import type { SkillCatalogEntry, SkillDefaults, SkillKind } from "./types";

/** The slash trigger word being typed at the end of the composer. */
const TRIGGER = /(?:^|\s)\/([A-Za-z0-9_-]*)$/;

export const EMPTY_SKILL_SELECTION: SkillDefaults = { workflow_ids: [], skill_ids: [] };

function skillSelectionKey(kind: SkillKind): keyof SkillDefaults {
  return kind === "workflow" ? "workflow_ids" : "skill_ids";
}

/** The trigger query the composer text ends with, or null when none is open. */
export function readSkillTrigger(message: string): string | null {
  const match = message.match(TRIGGER);
  return match ? match[1] : null;
}

export function filterSkillCatalog(
  catalog: SkillCatalogEntry[],
  query: string,
): SkillCatalogEntry[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return catalog;
  return catalog.filter(
    (item) =>
      item.id.includes(needle) ||
      item.label.toLowerCase().includes(needle) ||
      item.kind.includes(needle),
  );
}

/** Slash commands may invoke only packages explicitly enabled in Settings. */
export function filterSkillCatalogToDefaults(
  catalog: SkillCatalogEntry[],
  defaults: SkillDefaults,
): SkillCatalogEntry[] {
  const allowed = new Set([
    ...defaults.workflow_ids.map((id) => `workflow:${id}`),
    ...defaults.skill_ids.map((id) => `skill:${id}`),
  ]);
  return catalog.filter((item) => allowed.has(`${item.kind}:${item.id}`));
}

/** Wrap the highlight so arrowing past either end returns to the other. */
export function moveSkillHighlight(index: number, count: number, delta: number): number {
  if (count <= 0) return 0;
  return (((index + delta) % count) + count) % count;
}

export function addSkillSelection(
  selection: SkillDefaults,
  entry: SkillCatalogEntry,
): SkillDefaults {
  const key = skillSelectionKey(entry.kind);
  if (selection[key].includes(entry.id)) return selection;
  return { ...selection, [key]: [...selection[key], entry.id] };
}

export function removeSkillSelection(
  selection: SkillDefaults,
  kind: SkillKind,
  id: string,
): SkillDefaults {
  const key = skillSelectionKey(kind);
  return { ...selection, [key]: selection[key].filter((item) => item !== id) };
}

export function selectedSkillRefs(selection: SkillDefaults): [SkillKind, string][] {
  return [
    ...selection.workflow_ids.map((id) => ["workflow", id] as [SkillKind, string]),
    ...selection.skill_ids.map((id) => ["skill", id] as [SkillKind, string]),
  ];
}

export function hasSkillSelection(selection: SkillDefaults): boolean {
  return selection.workflow_ids.length > 0 || selection.skill_ids.length > 0;
}
