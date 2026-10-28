/** Whether following `href` in place would unload the app document: a link to another
 * origin or to any same-origin path other than this page. Hash routes stay in place. */
export function leavesApp(href: string, here: URL): boolean {
  const target = new URL(href, here);
  return target.origin !== here.origin || target.pathname !== here.pathname;
}

/** A link the page did not handle opens in a new tab instead of stranding the app on a
 * backend URL or a foreign page. Downloads and explicit new-tab links pass through. */
export function installLinkGuard() {
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0) return;
    const link = (event.target as Element | null)?.closest?.("a[href]");
    if (!(link instanceof HTMLAnchorElement) || link.hasAttribute("download") || link.target)
      return;
    if (!leavesApp(link.href, new URL(window.location.href))) return;
    event.preventDefault();
    window.open(link.href, "_blank", "noopener,noreferrer");
  });
}
