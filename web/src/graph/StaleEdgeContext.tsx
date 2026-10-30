import { createContext, useContext, type ReactNode } from "react";

interface StaleEdgeContextValue {
  /** Outcome-to-belief edges judged before their Evidence last moved. */
  staleEdgeIds: ReadonlySet<string>;
  /** The stale ones on main, where a light agent may re-judge them. */
  refreshableEdgeIds: ReadonlySet<string>;
  /** Start or reuse the edge's re-judging chat for the Evidence revision shown, and open it. */
  onRefresh: (edgeId: string, evidenceRev: number) => Promise<void>;
}

const StaleEdgeContext = createContext<StaleEdgeContextValue>({
  staleEdgeIds: new Set(),
  refreshableEdgeIds: new Set(),
  onRefresh: async () => {},
});

export function StaleEdgeProvider({
  children,
  ...value
}: StaleEdgeContextValue & { children?: ReactNode }) {
  return <StaleEdgeContext.Provider value={value}>{children}</StaleEdgeContext.Provider>;
}

export function useStaleEdges() {
  return useContext(StaleEdgeContext);
}
