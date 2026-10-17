import type { ProjectCard, ProjectGroup } from "../core/types";

export function projectGroupShelf(projects: ProjectCard[], groups: ProjectGroup[]) {
  return {
    ungrouped: projects.filter((project) => !project.group_id),
    groups: groups.map((group) => {
      const members = projects.filter((project) => project.group_id === group.id);
      return {
        ...group,
        projects: members,
        count: members.length,
        waiting: members.reduce((sum, project) => sum + project.attention_count, 0),
      };
    }),
  };
}

export function projectGroupHash(groupId: string | null): string {
  return groupId === null ? "#" : `#/project-groups/${encodeURIComponent(groupId)}`;
}

export function parseProjectGroupHash(hash: string): string | null {
  const match = /^#\/project-groups\/([^/?#]+)$/.exec(hash);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]) || null;
  } catch {
    return null;
  }
}

export function canDropProject(
  project: ProjectCard | undefined,
  groupId: string | null,
  groups: ProjectGroup[],
): boolean {
  return Boolean(
    project &&
    (project.group_id ?? null) !== groupId &&
    (groupId === null || groups.some((group) => group.id === groupId)),
  );
}

export const PROJECT_DRAG_TYPE = "application/x-rcp-project";
