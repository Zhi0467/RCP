import type { AppView } from "./types";

/** Sub-views that open inside another top tab; every other view is its own tab. */
const PARENT_TAB: Partial<Record<AppView, AppView>> = { dag: "scientific", paper: "artifacts" };

export const topTab = (view: AppView): AppView => PARENT_TAB[view] ?? view;

const subviewKey = (projectId: string, tab: AppView) => `rcp:project-subview:${projectId}:${tab}`;

/** Records the view this viewer has open, so its top tab reopens it in this project. */
export function rememberSubview(projectId: string, view: AppView): void {
  try {
    localStorage.setItem(subviewKey(projectId, topTab(view)), view);
  } catch {
    /* A remembered sub-view is a convenience. */
  }
}

/** The view a top tab opens on: the sub-view last open there in this project, else the tab. */
export function tabEntryView(projectId: string, tab: AppView): AppView {
  try {
    const stored = localStorage.getItem(subviewKey(projectId, tab)) as AppView | null;
    if (stored && topTab(stored) === tab) return stored;
  } catch {
    /* Fall back to the tab itself. */
  }
  return tab;
}
