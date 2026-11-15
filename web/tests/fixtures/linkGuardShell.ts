import { installLinkGuard } from "../../src/core/linkGuard";
import { currentShareLinkRequest } from "../../src/open/openLinkModel";

declare global {
  interface Window {
    shareRequest: () => ReturnType<typeof currentShareLinkRequest>;
  }
}

installLinkGuard();
window.shareRequest = currentShareLinkRequest;
document.body.dataset.ready = "true";
