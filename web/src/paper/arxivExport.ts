export function paperArxivUrl(projectId: string, fileId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/paper/export/arxiv?file_id=${encodeURIComponent(fileId)}`;
}

export type ArxivExport = { blob: Blob } | { reason: string; log_tail: string };

export async function fetchArxivExport(projectId: string, fileId: string): Promise<ArxivExport> {
  const response = await fetch(paperArxivUrl(projectId, fileId));
  if (response.status === 409) return response.json() as Promise<ArxivExport>;
  if (!response.ok) throw new Error(`Export failed (${response.status})`);
  return { blob: await response.blob() };
}
