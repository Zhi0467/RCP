import { useSyncExternalStore } from "react";
import { api, ApiError } from "../core/api";
import { readPref, writePref } from "../core/prefStorage";
import type { ResolvedShareLink, ShareLinkKind } from "../core/types";

/**
 * A share link is `https://<access_url>/open#l=<token>`. The token is read once
 * at load, like a pairing code, so the team sign-in in between keeps it. A link
 * clicked inside the app to this same space joins the same request in place.
 */

export type OpenChoice = "app" | "here" | "ask";
export const OPEN_CHOICES: readonly OpenChoice[] = ["app", "here", "ask"];
export const OPEN_CHOICE_KEY = "rcp.openShareLinks";

export function readOpenChoice(): OpenChoice {
  const stored = readPref(OPEN_CHOICE_KEY);
  return OPEN_CHOICES.includes(stored as OpenChoice) ? (stored as OpenChoice) : "ask";
}

export function writeOpenChoice(choice: OpenChoice): void {
  writePref(OPEN_CHOICE_KEY, choice === "ask" ? null : choice);
}

const TOKEN_HASH = /^#l=([A-Za-z0-9_-]+)$/;

export function shareTokenFromLocation(pathname: string, hash: string): string | null {
  return pathname === "/open" ? (TOKEN_HASH.exec(hash)?.[1] ?? null) : null;
}

/** The team host and token a copied link carries for the public page: `#s=…&h=…&l=…`. */
export function publicShareLink(hash: string): { host: string; token: string } | null {
  const fields = new URLSearchParams(hash.replace(/^#/, ""));
  const host = fields.get("h");
  const token = fields.get("l");
  return fields.get("s") && host && token && TOKEN_HASH.test(`#l=${token}`)
    ? { host, token }
    : null;
}

/** The token of a link to this same space: this origin, its team access origin, or a
 *  public-page link that names this space's host. */
export function sameSpaceShareToken(
  href: string,
  here: URL,
  accessOrigin: string | null,
): string | null {
  const target = new URL(href, here);
  const shared = publicShareLink(target.hash);
  if (shared) {
    const hosts = [here.host, accessOrigin ? new URL(accessOrigin).host : null];
    return hosts.includes(shared.host) ? shared.token : null;
  }
  if (target.origin !== here.origin && target.origin !== accessOrigin) return null;
  return shareTokenFromLocation(target.pathname, target.hash);
}

export function appHandoffUrl(spaceId: string, host: string, token: string): string {
  return `rcp://open?${new URLSearchParams({ space: spaceId, host, l: token })}`;
}

/** The desktop app ships for macOS only; an iPad also reports a Mac. */
export function offersOpenInApp(desktop: boolean, userAgent: string, touchPoints: number): boolean {
  return !desktop && /Macintosh/.test(userAgent) && touchPoints <= 1;
}

export function effectiveOpenChoice(choice: OpenChoice, offersApp: boolean): OpenChoice {
  return choice === "app" && !offersApp ? "ask" : choice;
}

export type OpenLinkStart = "resolve" | "signin" | "handoff" | "card";

/** The desktop shell and a chosen "Open here" never show the card. */
export function openLinkStart({
  desktop,
  signedIn,
  openHere,
  choice,
  offersApp,
}: {
  desktop: boolean;
  signedIn: boolean;
  openHere: boolean;
  choice: OpenChoice;
  offersApp: boolean;
}): OpenLinkStart {
  if (desktop || openHere || choice === "here") return signedIn ? "resolve" : "signin";
  return effectiveOpenChoice(choice, offersApp) === "app" ? "handoff" : "card";
}

export function openLinkFailure(desktop: boolean, error: unknown): "card" | "retry" | "notice" {
  if (error === null) return desktop ? "notice" : "card";
  return error instanceof ApiError && error.status === 503 ? "retry" : "notice";
}

const KIND_LABELS: Record<ShareLinkKind, string> = {
  artifact: "Artifact",
  node: "Node",
  paper: "Paper file",
  comment: "Comment",
  chat: "Chat",
  reply: "Reply",
};

export interface OpenLinkCardView {
  kind: ShareLinkKind | null;
  title: string;
  name: string | null;
  unavailable: boolean;
  app: boolean;
  here: boolean;
  remember: boolean;
  retry: boolean;
}

/**
 * What the /open card shows, or null for a blank page while a link resolves or
 * hands off. `resolved` is undefined until answered and null for a 404. A
 * signed-out visitor sees no item details.
 */
export function openLinkCardView({
  start,
  sessionKnown,
  signedIn,
  resolved,
  retry,
  fallback,
  offersApp,
  spaceName,
}: {
  start: OpenLinkStart;
  sessionKnown: boolean;
  signedIn: boolean;
  resolved: ResolvedShareLink | null | undefined;
  retry: boolean;
  fallback: boolean;
  offersApp: boolean;
  spaceName: string;
}): OpenLinkCardView | null {
  const card = {
    kind: null,
    title: spaceName,
    name: null,
    unavailable: false,
    app: false,
    here: false,
    remember: false,
    retry: false,
  };
  if (retry) return { ...card, retry: true };
  if (resolved === null) return { ...card, name: "Not available", unavailable: true };
  if (start === "handoff")
    return fallback ? { ...card, title: "Opening in RCP…", here: true } : null;
  if (start !== "card" || !sessionKnown) return null;
  const actions = { app: offersApp, here: true, remember: true };
  if (!signedIn) return { ...card, ...actions };
  if (!resolved) return null;
  return {
    ...card,
    ...actions,
    kind: resolved.kind,
    title: `${KIND_LABELS[resolved.kind]} in ${resolved.project_name}`,
    name: resolved.title,
  };
}

/** The item behind a token, or null for the one 404 every unreadable link answers. */
export async function resolveShareLink(token: string): Promise<ResolvedShareLink | null> {
  try {
    return await api<ResolvedShareLink>(
      "/api/share-links/resolve",
      { method: "POST", body: JSON.stringify({ token }) },
      { readOnly: true },
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

/** Hands the generic link to the app's notification resolver, leaving /open. */
export function openResolvedHere(link: string): void {
  const oldURL = window.location.href;
  window.history.replaceState(null, "", `/${link}`);
  window.dispatchEvent(new HashChangeEvent("hashchange", { oldURL, newURL: window.location.href }));
}

export interface ShareLinkRequest {
  token: string;
  /** Clicked inside the app, so it opens here without the /open card. */
  inPlace: boolean;
  /** Chosen on the card; kept here because sign-in remounts the app. */
  openHere: boolean;
}

let request: ShareLinkRequest | null = (() => {
  if (typeof window === "undefined") return null;
  const token = shareTokenFromLocation(window.location.pathname, window.location.hash);
  return token ? { token, inPlace: false, openHere: false } : null;
})();
let accessOrigin: string | null = null;
const listeners = new Set<() => void>();

function publish(next: ShareLinkRequest | null) {
  request = next;
  listeners.forEach((listener) => listener());
}

export function requestShareLinkInPlace(token: string): void {
  publish({ token, inPlace: true, openHere: true });
}

export function chooseOpenHere(): void {
  if (request && !request.openHere) publish({ ...request, openHere: true });
}

export function settleShareLink(): void {
  publish(null);
}

export function currentShareLinkRequest(): ShareLinkRequest | null {
  return request;
}

export function useShareLinkRequest(): ShareLinkRequest | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    () => request,
    () => null,
  );
}

/** The desktop shell matches links by the team's access origin, read once signed in. */
export function setTeamAccessOrigin(origin: string | null): void {
  accessOrigin = origin;
}

export function teamAccessOrigin(): string | null {
  return accessOrigin;
}
