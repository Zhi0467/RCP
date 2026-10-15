import { createContext, useContext, type ReactNode } from "react";

interface EvidenceFlagContextValue {
  nodeIds: ReadonlySet<string>;
  onOpenInbox: () => void;
}

const EvidenceFlagContext = createContext<EvidenceFlagContextValue>({
  nodeIds: new Set(),
  onOpenInbox: () => {},
});

export function EvidenceFlagProvider({
  children,
  ...value
}: EvidenceFlagContextValue & { children?: ReactNode }) {
  return <EvidenceFlagContext.Provider value={value}>{children}</EvidenceFlagContext.Provider>;
}

export function useEvidenceFlags() {
  return useContext(EvidenceFlagContext);
}
