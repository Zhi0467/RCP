/** HTTP failures which need a human retry, rather than another automatic tick. */
export function isPermanentArtifactError(status: number): boolean {
  return status >= 400 && status < 500 && ![408, 409, 425, 429].includes(status);
}

export type ArtifactPopupTarget =
  | { kind: "artifact"; projectId: string; artifactId: string }
  | { kind: "report"; projectId: string; episodeId: string };

/** Only viewer entrances on the active RCP origin can become panel targets. */
export function artifactPopupTarget(
  raw: string,
  origin: string,
  projectId: string | null,
): ArtifactPopupTarget | null {
  try {
    const url = new URL(raw);
    if (url.origin !== origin || !projectId) return null;
    const artifact =
      /^\/api\/projects\/([^/]+)\/artifacts\/([^/]+)\/(?:viewer|preview|content|download)$/.exec(
        url.pathname,
      );
    if (artifact && decodeURIComponent(artifact[1]) === projectId)
      return {
        kind: "artifact",
        projectId: decodeURIComponent(artifact[1]),
        artifactId: decodeURIComponent(artifact[2]),
      };
    const report =
      /^\/api\/projects\/([^/]+)\/episodes\/([^/]+)\/report\/(?:viewer|preview|content|download)$/.exec(
        url.pathname,
      );
    if (report && decodeURIComponent(report[1]) === projectId)
      return {
        kind: "report",
        projectId: decodeURIComponent(report[1]),
        episodeId: decodeURIComponent(report[2]),
      };
  } catch {
    // Malformed URLs are not viewer targets.
  }
  return null;
}
