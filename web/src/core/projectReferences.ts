import { buildNotificationLink, parseNotificationLink } from "../notificationLinks";
import type {
  GraphTargetRef,
  ProjectArtifact,
  ProjectReferenceSelector,
  ProjectReferenceSource,
} from "../types";

export const MAX_CHAT_ATTACHMENTS = 8;
export interface DraftReference {
  selector: ProjectReferenceSelector;
  label: string;
  target: GraphTargetRef;
}

export function referenceKey(selector: ProjectReferenceSelector): string {
  return JSON.stringify(
    selector.kind === "node"
      ? [selector.kind, selector.branch_id, selector.node_id]
      : [selector.kind, selector.kind === "artifact" ? selector.artifact_id : "introduction"],
  );
}

export function referenceUrl(
  projectId: string,
  target: GraphTargetRef,
  selector: ProjectReferenceSelector,
): string {
  const sourceTarget =
    selector.kind === "node"
      ? (selector.branch_id ?? "main")
      : target.kind === "branch"
        ? target.branch_id
        : "main";
  const hash = buildNotificationLink({
    projectId,
    target: sourceTarget,
    kind: selector.kind,
    itemId:
      selector.kind === "node"
        ? selector.node_id
        : selector.kind === "artifact"
          ? selector.artifact_id
          : "introduction",
  });
  return `${window.location.href.split("#")[0]}${hash}`;
}

export function setReferenceDrag(
  transfer: DataTransfer,
  projectId: string,
  target: GraphTargetRef,
  selector: ProjectReferenceSelector,
): void {
  const url = referenceUrl(projectId, target, selector);
  transfer.setData("text/uri-list", url);
  transfer.setData("text/plain", url);
  transfer.effectAllowed = "copy";
}

export function mergeReferences(
  current: DraftReference[],
  incoming: DraftReference[],
  attachmentCount: number,
): { references: DraftReference[]; rejected: number } {
  const references = [...current];
  const keys = new Set(current.map((item) => referenceKey(item.selector)));
  let rejected = 0;
  for (const item of incoming) {
    const key = referenceKey(item.selector);
    if (keys.has(key)) continue;
    if (references.length + attachmentCount >= MAX_CHAT_ATTACHMENTS) {
      rejected++;
      continue;
    }
    keys.add(key);
    references.push(item);
  }
  return { references, rejected };
}

/** Only consume admitted same-project links; all other pasted characters survive. */
export function extractReferences(
  text: string,
  projectId: string,
  current: DraftReference[] = [],
  attachmentCount = 0,
  labelFor: (selector: ProjectReferenceSelector) => string = referenceFallbackLabel,
): { text: string; references: DraftReference[]; rejected: number } {
  let references = current;
  let rejected = 0;
  const remaining = text.replace(
    /(?:[a-z][a-z\d+.-]*:\/\/[^\s<>"']*?)?#\/projects\/[^\s<>"']+/gi,
    (url) => {
      // A prose delimiter is not part of a copied encoded segment.
      const suffix = url.match(/[).,;!]+$/)?.[0] ?? "";
      const link = parseNotificationLink(suffix ? url.slice(0, -suffix.length) : url);
      if (
        !link ||
        link.projectId !== projectId ||
        !["artifact", "node", "paper"].includes(link.kind)
      )
        return url;
      const target: GraphTargetRef =
        link.target === "main" ? { kind: "main" } : { kind: "branch", branch_id: link.target };
      const selector: ProjectReferenceSelector =
        link.kind === "artifact"
          ? { kind: "artifact", artifact_id: link.itemId }
          : link.kind === "node"
            ? {
                kind: "node",
                node_id: link.itemId,
                branch_id: link.target === "main" ? null : link.target,
              }
            : { kind: "paper" };
      const result = mergeReferences(
        references,
        [{ selector, target, label: labelFor(selector) }],
        attachmentCount,
      );
      references = result.references;
      rejected += result.rejected;
      return result.rejected ? url : suffix;
    },
  );
  return { text: remaining, references, rejected };
}

/** A link names no title; the sent turn shows the server's display name. */
export function referenceFallbackLabel(selector: ProjectReferenceSelector): string {
  if (selector.kind === "paper") return "Paper introduction";
  if (selector.kind === "node") return selector.node_id;
  return `Artifact ${selector.artifact_id.slice(0, 6)}`;
}

/** Artifact ids still showing the link fallback, so the composer can look up their names. */
export function unlabeledArtifactIds(references: readonly DraftReference[]): string[] {
  return references.flatMap((item) =>
    item.selector.kind === "artifact" && item.label === referenceFallbackLabel(item.selector)
      ? [item.selector.artifact_id]
      : [],
  );
}

/** Name pasted or dropped artifact chips from the saved inventory; unknown ids keep the fallback. */
export function labelArtifactReferences(
  references: DraftReference[],
  artifacts: readonly Pick<ProjectArtifact, "artifact_id" | "name">[],
): DraftReference[] {
  const names = new Map(artifacts.map((item) => [item.artifact_id, item.name]));
  let changed = false;
  const labeled = references.map((item) => {
    const name =
      item.selector.kind === "artifact" && item.label === referenceFallbackLabel(item.selector)
        ? names.get(item.selector.artifact_id)
        : undefined;
    if (!name) return item;
    changed = true;
    return { ...item, label: name };
  });
  return changed ? labeled : references;
}

export function referenceDraftKey(
  projectId: string,
  target: GraphTargetRef,
  chatId: string,
): string {
  return `rcp:chat-inputs:${JSON.stringify([projectId, target.kind === "branch" ? target.branch_id : null, chatId])}`;
}

export function parseReferenceDraft(value: string | null): DraftReference[] {
  try {
    const items: unknown = JSON.parse(value ?? "[]");
    if (!Array.isArray(items)) return [];
    const valid = items.filter((item): item is DraftReference => {
      const s = item?.selector;
      return (
        typeof item?.label === "string" &&
        (item?.target?.kind === "main" ||
          (item?.target?.kind === "branch" && typeof item.target.branch_id === "string")) &&
        (s?.kind === "paper" ||
          (s?.kind === "artifact" && typeof s.artifact_id === "string") ||
          (s?.kind === "node" &&
            typeof s.node_id === "string" &&
            (s.branch_id === null || typeof s.branch_id === "string")))
      );
    });
    return mergeReferences([], valid, 0).references;
  } catch {
    return [];
  }
}

export function sourceReference(source: ProjectReferenceSource): DraftReference {
  const target = source.graph_head?.target ?? { kind: "main" as const };
  return {
    target,
    label: source.source_id,
    selector:
      source.kind === "artifact"
        ? { kind: "artifact", artifact_id: source.source_id }
        : source.kind === "node"
          ? {
              kind: "node",
              node_id: source.source_id,
              branch_id: target.kind === "branch" ? target.branch_id : null,
            }
          : { kind: "paper" },
  };
}
