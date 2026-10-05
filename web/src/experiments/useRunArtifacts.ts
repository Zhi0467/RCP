import { useEffect, useState } from "react";
import { api } from "../api";
import { errorMessage } from "../errors";
import { orderRunArtifacts } from "./runArtifacts";
import type { RunArtifactEntry } from "../types";

export function useRunArtifacts(
  apiBase: string,
  episodeId: string | undefined,
  visible: boolean,
  revision: string,
) {
  const key = `${apiBase}:${episodeId ?? ""}`;
  const [result, setResult] = useState<{
    key: string;
    artifacts: RunArtifactEntry[];
    error: string | null;
  } | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!visible || !episodeId) return;
    const controller = new AbortController();
    void api<RunArtifactEntry[]>(`${apiBase}/episodes/${encodeURIComponent(episodeId)}/artifacts`, {
      signal: controller.signal,
    }).then(
      (artifacts) => {
        if (!controller.signal.aborted)
          setResult({ key, artifacts: orderRunArtifacts(artifacts), error: null });
      },
      (failure) => {
        if (!controller.signal.aborted)
          setResult((previous) => ({
            key,
            artifacts: previous?.key === key ? previous.artifacts : [],
            error: errorMessage(failure),
          }));
      },
    );
    return () => controller.abort();
  }, [apiBase, episodeId, visible, revision, key, retry]);
  return {
    artifacts: result?.key === key ? result.artifacts : [],
    error: result?.key === key ? result.error : null,
    loading: Boolean(visible && episodeId && result?.key !== key),
    reload: () => setRetry((value) => value + 1),
  };
}
