import { isDesktopRuntime } from "./desktopRuntime";
import {
  requestShareLinkInPlace,
  sameSpaceShareToken,
  teamAccessOrigin,
} from "../open/openLinkModel";

/** Whether following `href` in place would unload the app document: a link to another
 * origin or to any same-origin path other than this page. Hash routes stay in place. */
export function leavesApp(href: string, here: URL): boolean {
  const target = new URL(href, here);
  return target.origin !== here.origin || target.pathname !== here.pathname;
}

export type LinkClick = {
  href: string;
  target: string;
  download: boolean;
  modified: boolean;
};

/** What a plain click on an unhandled link does: a share link to this same space opens
 * in place, even from a rendered link that names a target; a link that would unload the
 * app opens in a new tab. Downloads, modified clicks, and explicit targets pass through. */
export function linkClickAction(
  click: LinkClick,
  here: URL,
  accessOrigin: string | null,
): { kind: "share"; token: string } | { kind: "new-tab" } | { kind: "pass" } {
  if (click.download) return { kind: "pass" };
  const token = sameSpaceShareToken(click.href, here, accessOrigin);
  if (token && !click.modified) return { kind: "share", token };
  if (click.target || !leavesApp(click.href, here)) return { kind: "pass" };
  return { kind: "new-tab" };
}

function linkClick(event: MouseEvent): { link: HTMLAnchorElement; click: LinkClick } | null {
  if (event.button !== 0) return null;
  const link = (event.target as Element | null)?.closest?.("a[href]");
  if (!(link instanceof HTMLAnchorElement)) return null;
  return {
    link,
    click: {
      href: link.href,
      target: link.target,
      download: link.hasAttribute("download"),
      modified: event.metaKey || event.ctrlKey || event.shiftKey,
    },
  };
}

/** Share links are taken in the capture phase: the desktop shell's own handler cancels
 * every `_blank` link click and then fails to open it. In the desktop app a `_blank`
 * link therefore loses its target for this one click, so that handler skips it and
 * the bubbling listener opens it through the window the shell routes. */
export function installLinkGuard() {
  window.addEventListener(
    "click",
    (event) => {
      const found = linkClick(event);
      if (!found) return;
      const action = linkClickAction(
        found.click,
        new URL(window.location.href),
        teamAccessOrigin(),
      );
      if (action.kind === "share") {
        event.preventDefault();
        event.stopImmediatePropagation();
        requestShareLinkInPlace(action.token);
        return;
      }
      if (isDesktopRuntime() && found.link.target === "_blank" && !found.click.download) {
        found.link.removeAttribute("target");
        setTimeout(() => found.link.setAttribute("target", "_blank"));
      }
    },
    true,
  );
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented) return;
    const found = linkClick(event);
    if (!found) return;
    const action = linkClickAction(found.click, new URL(window.location.href), teamAccessOrigin());
    if (action.kind !== "new-tab") return;
    event.preventDefault();
    window.open(found.link.href, "_blank", "noopener,noreferrer");
  });
}
