// A notification carries one generic link, `#/projects/{p}/targets/{t}/{kind}/{id}`,
// built by the backend without knowing Web routes. App resolves it once into the
// ordinary route for that item, so route construction stays in one place.

import {
  AUTO_RESEARCH_ROUTE_PREFIX,
  experimentBoardHref,
  experimentBoardRouteToken,
} from "../experimentBoard";
import type { Episode, ExperimentLoopIndexEntry } from "../types";

export type NotificationItemKind =
  "proposal" | "decision" | "blocker" | "episode" | "artifact" | "node" | "paper" | "consolidation";

export interface NotificationLink {
  projectId: string;
  target: string;
  kind: NotificationItemKind;
  itemId: string;
}

const LINK_PATTERN =
  /^#\/projects\/([^/?#]+)\/targets\/([^/?#]+)\/(proposal|decision|blocker|episode|artifact|node|paper|consolidation)\/([^/?#]+)$/;

export function buildNotificationLink(link: NotificationLink): string {
  const segment = (value: string) =>
    encodeURIComponent(value).replace(
      /[.!'()*]/g,
      (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`,
    );
  return `#/projects/${segment(link.projectId)}/targets/${segment(link.target)}/${segment(link.kind)}/${segment(link.itemId)}`;
}

export function parseNotificationLink(hash: string): NotificationLink | null {
  const match = LINK_PATTERN.exec(hash.slice(hash.indexOf("#")));
  if (!match || (match[3] === "paper" && match[4] !== "introduction")) return null;
  try {
    return {
      projectId: decodeURIComponent(match[1]),
      target: decodeURIComponent(match[2]),
      kind: match[3] as NotificationItemKind,
      itemId: decodeURIComponent(match[4]),
    };
  } catch {
    return null;
  }
}

/**
 * Graph items and consolidation rows open in the Inbox; App then opens a graph
 * node itself, resolved or not.
 */
export function graphNotificationHash(link: NotificationLink): string {
  return `#/projects/${encodeURIComponent(link.projectId)}?view=attention`;
}

/** An episode opens its exact run when it is still listed, else the Runs view. */
export function episodeNotificationHash(
  link: NotificationLink,
  episode: Episode | null,
  experimentEntries: ExperimentLoopIndexEntry[],
): string {
  return episodeRunHash(link.projectId, link.itemId, episode, experimentEntries);
}

/** The exact run route for one episode id: an Auto-research route, or the Experiment's board entry. */
export function episodeRunHash(
  projectId: string,
  episodeId: string,
  episode: Episode | null,
  experimentEntries: ExperimentLoopIndexEntry[],
): string {
  if (episode?.mode === "auto_research") {
    return experimentBoardHref(projectId, `${AUTO_RESEARCH_ROUTE_PREFIX}${episode.episode_id}`);
  }
  const entry = experimentEntries.find((item) => item.episode?.episode_id === episodeId);
  return entry
    ? experimentBoardHref(projectId, experimentBoardRouteToken(entry))
    : `#/projects/${encodeURIComponent(projectId)}?view=runs`;
}

// Read once at load, like a pairing code, so a sign-in in between keeps it.
export const initialNotificationLink: NotificationLink | null =
  typeof window === "undefined" ? null : parseNotificationLink(window.location.hash);
