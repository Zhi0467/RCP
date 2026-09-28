// Notification devices: this browser's Web Push subscription and the calls
// that list, test, and remove devices. A device turns on only from a tap on
// its row, so nothing here asks for permission on page load.

import { api } from "./api";

export interface NotificationDevice {
  device_id: string;
  kind: "desktop" | "web_push";
  session_id: string | null;
  status: "on" | "delivery_failed";
  created_at: string;
}

export interface PhonePairing {
  code: string;
  expires_at: string;
  listener_port: number;
}

/** Detect Web Push by capability, never by device brand. */
export function webPushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

export function loadNotificationDevices(): Promise<NotificationDevice[]> {
  return api<NotificationDevice[]>("/api/notifications/devices");
}

export function removeNotificationDevice(deviceId: string): Promise<{ ok: boolean }> {
  return api(`/api/notifications/devices/${encodeURIComponent(deviceId)}`, { method: "DELETE" });
}

export function testNotificationDevice(deviceId: string): Promise<{ outcome: string }> {
  return api(`/api/notifications/devices/${encodeURIComponent(deviceId)}/test`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function createPhonePairing(): Promise<PhonePairing> {
  return api<PhonePairing>("/api/notifications/phone-pairings", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

async function registerSubscription(subscription: PushSubscription): Promise<NotificationDevice> {
  return api<NotificationDevice>("/api/notifications/devices/web-push", {
    method: "POST",
    body: JSON.stringify(subscription.toJSON()),
  });
}

/** Ask for permission, subscribe this browser, and register it for this session. */
export async function enableWebPush(): Promise<void> {
  if ((await Notification.requestPermission()) !== "granted") {
    throw new Error("Notifications were not allowed in this browser.");
  }
  const registration = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
  await navigator.serviceWorker.ready;
  const { application_server_key } = await api<{ application_server_key: string }>(
    "/api/notifications/web-push/key",
  );
  const subscription =
    (await registration.pushManager.getSubscription()) ??
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: application_server_key,
    }));
  await registerSubscription(subscription);
}

export async function disableWebPush(device: NotificationDevice | null): Promise<void> {
  const registration = await navigator.serviceWorker.getRegistration("/");
  await (await registration?.pushManager.getSubscription())?.unsubscribe();
  if (device) await removeNotificationDevice(device.device_id);
}

/**
 * On each signed-in visit, bring the server in line with this browser: a live
 * subscription re-registers (keeping its device), and a server device whose
 * browser subscription is gone is removed.
 */
export async function reconcileWebPush(currentDevice: NotificationDevice | null): Promise<void> {
  if (!webPushSupported()) return;
  const registration = await navigator.serviceWorker.getRegistration("/");
  const subscription = await registration?.pushManager.getSubscription();
  if (subscription && Notification.permission === "granted") {
    await registerSubscription(subscription);
  } else if (currentDevice) {
    await removeNotificationDevice(currentDevice.device_id);
  }
}
