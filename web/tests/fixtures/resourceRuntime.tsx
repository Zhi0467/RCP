import type { ReactNode } from "react";
import { ResourceStore } from "../../src/core/resourceStore";
import { RefreshRuntimeContext } from "../../src/core/useResource";

const store = new ResourceStore();
export function ResourceRuntime({ children }: { children: ReactNode }) {
  return (
    <RefreshRuntimeContext.Provider value={{ store } as never}>
      {children}
    </RefreshRuntimeContext.Provider>
  );
}
