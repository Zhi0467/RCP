/** A first Delete click arms the control; this long without a second click disarms it. */
export const DELETE_CONFIRM_MS = 4000;

export function artifactDeleteUrl(projectId: string, artifactId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}`;
}

/** 404 and 410 mean the artifact is already gone, which is what Delete wanted. */
export function artifactAlreadyGone(failure: unknown): boolean {
  const status = (failure as { status?: unknown } | null)?.status;
  return status === 404 || status === 410;
}
