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

export function installLinkGuard() {
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0) return;
    const link = (event.target as Element | null)?.closest?.("a[href]");
    if (!(link instanceof HTMLAnchorElement)) return;
    const action = linkClickAction(
      {
        href: link.href,
        target: link.target,
        download: link.hasAttribute("download"),
        modified: event.metaKey || event.ctrlKey || event.shiftKey,
      },
      new URL(window.location.href),
      teamAccessOrigin(),
    );
    if (action.kind === "pass") return;
    event.preventDefault();
    if (action.kind === "share") requestShareLinkInPlace(action.token);
    else window.open(link.href, "_blank", "noopener,noreferrer");
  });
}
