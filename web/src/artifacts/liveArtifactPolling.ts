export const LIVE_ARTIFACT_POLL_INTERVAL_MS = 1_500;

interface LiveArtifactPollingClock {
  setTimeout(callback: () => void, delay: number): number;
  clearTimeout(timeoutId: number): void;
}

export function startLiveArtifactPolling(
  clock: LiveArtifactPollingClock,
  refresh: () => Promise<void>,
  onError: (error: unknown) => void,
  onSuccess: () => void,
): () => void {
  let stopped = false;
  let timeoutId = 0;
  const schedule = () => {
    timeoutId = clock.setTimeout(() => void poll(), LIVE_ARTIFACT_POLL_INTERVAL_MS);
  };
  const poll = async () => {
    try {
      await refresh();
      if (!stopped) onSuccess();
    } catch (error) {
      if (!stopped) onError(error);
    } finally {
      if (!stopped) schedule();
    }
  };
  schedule();
  return () => {
    stopped = true;
    clock.clearTimeout(timeoutId);
  };
}
