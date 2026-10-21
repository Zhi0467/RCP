/** What the DAG Lens and the node bulbs leave visible and bright, remembered per project. */
export interface DagLens {
  brightTypes: ReadonlySet<string>;
  hiddenTypes: ReadonlySet<string>;
  litNodeIds: ReadonlySet<string>;
}

interface LensNode {
  id: string;
  type: string;
}

/** A graph opens dim: past a handful of nodes, reading starts from what the member lights. */
export const EMPTY_DAG_LENS: DagLens = {
  brightTypes: new Set(),
  hiddenTypes: new Set(),
  litNodeIds: new Set(),
};

export function lensShows(lens: DagLens, node: LensNode | undefined): boolean {
  return Boolean(node) && !lens.hiddenTypes.has(node!.type);
}

export function lensBrightNode(lens: DagLens, node: LensNode): boolean {
  return lens.litNodeIds.has(node.id) || lens.brightTypes.has(node.type);
}

/** A lit node brightens its own edges, never the nodes at their other ends. */
export function lensBrightEdge(lens: DagLens, source: LensNode, target: LensNode): boolean {
  return (
    lens.litNodeIds.has(source.id) ||
    lens.litNodeIds.has(target.id) ||
    (lens.brightTypes.has(source.type) && lens.brightTypes.has(target.type))
  );
}

export function toggled(values: ReadonlySet<string>, value: string): Set<string> {
  const next = new Set(values);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}

const storageKey = (projectId: string) => `rcp:dag-lens:${projectId}`;

export function readDagLens(projectId: string): DagLens {
  try {
    const stored = JSON.parse(window.localStorage.getItem(storageKey(projectId)) ?? "null");
    if (!stored) return EMPTY_DAG_LENS;
    return {
      brightTypes: new Set(stored.bright),
      hiddenTypes: new Set(stored.hidden),
      litNodeIds: new Set(stored.lit),
    };
  } catch {
    return EMPTY_DAG_LENS;
  }
}

export function writeDagLens(projectId: string, lens: DagLens): void {
  try {
    window.localStorage.setItem(
      storageKey(projectId),
      JSON.stringify({
        bright: [...lens.brightTypes],
        hidden: [...lens.hiddenTypes],
        lit: [...lens.litNodeIds],
      }),
    );
  } catch {
    // The lens still works for this visit when browser storage is unavailable.
  }
}
