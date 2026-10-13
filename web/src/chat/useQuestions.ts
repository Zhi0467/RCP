import { useCallback } from "react";
import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime } from "../core/useResource";
import {
  projectIdFromApiBase,
  questionsProfile,
  useProjectResource,
} from "../projects/projectResources";

/** Questions share the project's resource owner and scheduler. */
export function useQuestions(
  apiBase: string,
  kind: "chat" | "episode",
  ownerId: string | undefined,
) {
  const { store } = useRefreshRuntime();
  const profile = ownerId ? questionsProfile(projectIdFromApiBase(apiBase), kind, ownerId) : null;
  const view = useProjectResource(profile);
  const id = profile ? resourceKeyId(profile.key) : null;
  const refresh = useCallback(() => {
    if (id) store.invalidate(id);
  }, [store, id]);
  return {
    questions: view.value ?? [],
    error: view.error
      ? view.error instanceof Error
        ? view.error.message
        : String(view.error)
      : null,
    refresh,
  };
}
