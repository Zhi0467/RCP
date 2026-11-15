import { useEffect, useState } from "react";
import {
  Files,
  FileText,
  GitCommitVertical,
  Link2,
  MessageSquare,
  MessageSquareText,
  MessagesSquare,
  RefreshCw,
  type LucideIcon,
} from "lucide-react";
import type { ResolvedShareLink, ShareLinkKind } from "../core/types";
import { OPEN_IN_APP_FALLBACK_MS } from "../core/uiConstants";
import {
  appHandoffUrl,
  offersOpenInApp,
  openLinkCardView,
  openLinkFailure,
  openLinkStart,
  openResolvedHere,
  readOpenChoice,
  resolveShareLink,
  writeOpenChoice,
  type OpenLinkCardView,
} from "./openLinkModel";

const KIND_ICONS: Record<ShareLinkKind, LucideIcon> = {
  artifact: Files,
  node: GitCommitVertical,
  paper: FileText,
  comment: MessageSquare,
  chat: MessagesSquare,
  reply: MessageSquareText,
};

/** The /open page: the team sign-in card frame offering where a share link opens. */
export function OpenLinkPage({
  token,
  desktop,
  sessionKnown,
  signedIn,
  openHere,
  spaceId,
  spaceName,
  onOpenHere,
  onOpened,
  onUnavailable,
}: {
  token: string;
  desktop: boolean;
  sessionKnown: boolean;
  signedIn: boolean;
  openHere: boolean;
  spaceId: string | null;
  spaceName: string | null;
  onOpenHere: () => void;
  onOpened: () => void;
  onUnavailable: () => void;
}) {
  const [choice] = useState(readOpenChoice);
  const [resolved, setResolved] = useState<ResolvedShareLink | null | undefined>(undefined);
  const [retry, setRetry] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [fallback, setFallback] = useState(false);
  const [remember, setRemember] = useState(false);
  const offersApp =
    offersOpenInApp(desktop, navigator.userAgent, navigator.maxTouchPoints) && !!spaceId;
  const start = openLinkStart({ desktop, signedIn, openHere, choice, offersApp });

  useEffect(() => {
    if (start === "signin") onOpenHere();
  }, [start, onOpenHere]);

  useEffect(() => {
    if (!signedIn || start === "handoff" || start === "signin") return;
    let live = true;
    const failed = (error: unknown) => {
      const failure = openLinkFailure(desktop, error);
      if (failure === "card") setResolved(null);
      else if (failure === "retry") setRetry(true);
      else onUnavailable();
    };
    resolveShareLink(token).then(
      (link) => {
        if (!live) return;
        if (!link) failed(null);
        else if (start === "resolve") {
          openResolvedHere(link.link);
          onOpened();
        } else setResolved(link);
      },
      (error: unknown) => live && failed(error),
    );
    return () => {
      live = false;
    };
  }, [signedIn, start, token, desktop, attempt, onOpened, onUnavailable]);

  useEffect(() => {
    if (start !== "handoff" || !spaceId) return;
    window.location.href = appHandoffUrl(spaceId, window.location.host, token);
    const timer = window.setTimeout(() => setFallback(true), OPEN_IN_APP_FALLBACK_MS);
    return () => window.clearTimeout(timer);
  }, [start, spaceId, token]);

  const shown = openLinkCardView({
    start,
    sessionKnown,
    signedIn,
    resolved,
    retry,
    fallback,
    offersApp,
    spaceName: spaceName?.trim() || "Team space",
  });

  const openInApp = () => {
    if (!spaceId) return;
    if (remember) writeOpenChoice("app");
    window.location.href = appHandoffUrl(spaceId, window.location.host, token);
  };
  const openHereNow = () => {
    if (remember) writeOpenChoice("here");
    if (resolved) {
      openResolvedHere(resolved.link);
      onOpened();
    } else onOpenHere();
  };

  return (
    <main className="team-login-boundary">
      {shown && (
        <OpenLinkCard
          view={shown}
          remember={remember}
          onRemember={setRemember}
          onOpenInApp={openInApp}
          onOpenHere={openHereNow}
          onRetry={() => {
            setRetry(false);
            setAttempt((count) => count + 1);
          }}
        />
      )}
    </main>
  );
}

export function OpenLinkCard({
  view,
  remember,
  onRemember,
  onOpenInApp,
  onOpenHere,
  onRetry,
}: {
  view: OpenLinkCardView;
  remember: boolean;
  onRemember: (remember: boolean) => void;
  onOpenInApp: () => void;
  onOpenHere: () => void;
  onRetry: () => void;
}) {
  const Icon = view.kind ? KIND_ICONS[view.kind] : Link2;
  return (
    <section className="team-login-card open-link-card" aria-labelledby="open-link-title">
      <header>
        <span className="team-login-mark" aria-hidden="true">
          RCP
        </span>
        <span className="team-login-space-kind">Team space</span>
      </header>
      <div className="team-login-card-body">
        <span
          className={`open-link-kind${view.unavailable ? " unavailable" : ""}`}
          aria-hidden="true"
        >
          <Icon size={20} />
        </span>
        <h1 id="open-link-title" className={view.name ? "open-link-title" : undefined}>
          {view.title}
        </h1>
        {view.name && (
          <p
            className={`open-link-name${view.unavailable ? " unavailable" : ""}`}
            role={view.unavailable ? "alert" : undefined}
          >
            {view.name}
          </p>
        )}
        {(view.app || view.here) && (
          <div className={`open-link-actions${view.app && view.here ? "" : " single"}`}>
            {view.app && (
              <button className="button primary" type="button" onClick={onOpenInApp}>
                Open in app
              </button>
            )}
            {view.here && (
              <button
                className={`button${view.app ? "" : " primary"}`}
                type="button"
                onClick={onOpenHere}
              >
                Open here
              </button>
            )}
          </div>
        )}
        {view.retry && (
          <div className="open-link-actions single">
            <button
              className="button primary"
              type="button"
              title="The team space is briefly busy"
              onClick={onRetry}
            >
              <RefreshCw size={14} aria-hidden="true" /> Retry
            </button>
          </div>
        )}
        {view.remember && (
          <label
            className="open-link-remember"
            title="Later links open the same way. Change it in Settings."
          >
            <input
              type="checkbox"
              checked={remember}
              onChange={(event) => onRemember(event.target.checked)}
            />
            Remember
          </label>
        )}
      </div>
    </section>
  );
}
