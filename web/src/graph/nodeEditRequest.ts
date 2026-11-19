import { useSyncExternalStore } from "react";
import { sameGraphTarget } from "../core/graphTarget";
import type { GraphTargetRef } from "../core/types";

/**
 * A surface outside the graph asks for one node's Edit node. It opens the node
 * through its ordinary route, then the drawer showing that node takes this
 * request once and begins editing. A newer request replaces an untaken one.
 */
export interface NodeEditRequest {
  projectId: string;
  graphTarget: GraphTargetRef;
  nodeId: string;
}

let request: NodeEditRequest | null = null;
const listeners = new Set<() => void>();

function publish(next: NodeEditRequest | null) {
  request = next;
  listeners.forEach((listener) => listener());
}

export function requestNodeEdit(next: NodeEditRequest): void {
  publish(next);
}

/** Whether this exact node was asked for, cleared as it is answered. */
export function takeNodeEditRequest(
  projectId: string,
  graphTarget: GraphTargetRef,
  nodeId: string,
): boolean {
  if (
    request?.projectId !== projectId ||
    request.nodeId !== nodeId ||
    !sameGraphTarget(request.graphTarget, graphTarget)
  )
    return false;
  publish(null);
  return true;
}

/** Rerenders a mounted drawer when a request arrives for the node it already shows. */
export function useNodeEditRequest(): NodeEditRequest | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    () => request,
    () => null,
  );
}
