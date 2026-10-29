import type { AppView } from "./types";

/** Sub-views that open inside another top tab; every other view is its own tab. */
const PARENT_TAB: Partial<Record<AppView, AppView>> = { dag: "scientific", paper: "artifacts" };

export const topTab = (view: AppView): AppView => PARENT_TAB[view] ?? view;
const TABS_WITH_SUBVIEWS = new Set(Object.values(PARENT_TAB));

const subviewKey = (projectId: string, tab: AppView) => `rcp:project-subview:${projectId}:${tab}`;

/** Records the view open in a tab that has sub-views, so the tab reopens it in this project. */
export function rememberSubview(projectId: string, view: AppView): void {
  if (!TABS_WITH_SUBVIEWS.has(topTab(view))) return;
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

// Agents' sub-view is the chat open there.
const openChatKey = (projectId: string) => `${subviewKey(projectId, "chats")}:chat`;

/** Records the chat open in Agents, or null for its board, so Agents reopens it here. */
export function rememberOpenChat(projectId: string, chatId: string | null): void {
  try {
    if (chatId) localStorage.setItem(openChatKey(projectId), chatId);
    else localStorage.removeItem(openChatKey(projectId));
  } catch {
    /* A remembered chat is a convenience. */
  }
}

/** The chat Agents last had open in this project, if any. */
export function rememberedChat(projectId: string): string | null {
  try {
    return localStorage.getItem(openChatKey(projectId));
  } catch {
    return null;
  }
}
