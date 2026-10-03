// RCP service worker: shows pushed notifications and opens their item.
// A payload carries only a reason code, the project name, and, for a device
// that may open items, a hash-route link; never authored text.

const REASONS = {
  proposal: "A Proposal is waiting for you",
  decision: "A Decision is waiting for a choice",
  blocker: "A Blocker is open",
  episode_needs_action: "An episode needs you",
  episode_finished: "An episode finished",
  consolidation: "Nightly consolidation needs you",
  test: "Notifications are on",
};

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = {};
  }
  const title = typeof payload.project_name === "string" ? payload.project_name : "RCP";
  const body = REASONS[payload.reason] ?? "Something needs you in RCP";
  const options = {
    body,
    icon: "/icon-180.png",
    data: { deep_link: typeof payload.deep_link === "string" ? payload.deep_link : null },
  };
  // The stable id replaces a repeat instead of stacking it.
  if (typeof payload.notification_id === "string") {
    options.tag = payload.notification_id;
  }
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const link = event.notification.data?.deep_link;
  if (typeof link !== "string" || !link.startsWith("#/")) {
    return;
  }
  const target = new URL(`/${link}`, self.location.origin).href;
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of windows) {
        if (new URL(client.url).origin === self.location.origin) {
          await client.focus();
          await client.navigate(target);
          return;
        }
      }
      await self.clients.openWindow(target);
    })(),
  );
});
