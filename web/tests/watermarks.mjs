/**
 * Answer a `POST /api/resource-watermarks` body the way the server does: every
 * subscribed resource of every due project, versioned by `version`. A fixture
 * changes a resource by returning a new version for it, as a server counter would.
 */
export function watermarkResponse(request, { version = () => "v1" } = {}) {
  const open = new Map(request.open.map((project) => [project.project_id, project]));
  const due = request.due.filter((id) => open.has(id));
  return {
    epoch: "fixture",
    as_of: new Date().toISOString(),
    projects: due.map((id) => ({
      requested_id: id,
      project_id: id,
      resources: open.get(id).resources.map((resource) => ({
        ...resource,
        version: version(id, resource),
        valid_until: null,
      })),
      sync: { last_completed_at: null, status: "local", error: null },
    })),
    unavailable: [],
  };
}
