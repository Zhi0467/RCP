import { useId } from "react";
import { runArtifactsProfile } from "../artifacts/artifactResources";
import { errorMessage } from "../core/errors";
import { resourceKeyId } from "../core/resourceStore";
import { useResource, useRefreshRuntime } from "../core/useResource";

export function useRunArtifacts(
  projectId: string | undefined,
  episodeId: string | undefined,
  visible: boolean,
) {
  const owner = useId();
  const { store } = useRefreshRuntime();
  const profile =
    projectId && episodeId && visible ? runArtifactsProfile(projectId, episodeId) : null;
  const resource = useResource(owner, profile);
  return {
    artifacts: resource.value ?? [],
    error: resource.error ? errorMessage(resource.error) : null,
    loading: Boolean(profile && resource.value === undefined && !resource.error),
    reload: () => {
      if (profile) store.invalidate(resourceKeyId(profile.key));
    },
  };
}
