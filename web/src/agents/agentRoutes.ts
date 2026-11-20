export function threadPath(threadId: string): string {
  return `/api/agent-threads/${encodeURIComponent(threadId)}`;
}

export function projectAgentPath(apiBase: string, agentId: string, action: string): string {
  const projectId = decodeURIComponent(apiBase.slice(apiBase.lastIndexOf("/") + 1));
  return `${threadPath(agentId)}/${action}?${new URLSearchParams({ project_id: projectId })}`;
}
