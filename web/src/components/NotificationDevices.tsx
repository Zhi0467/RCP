import { Bell, Smartphone } from "lucide-react";
import { useCallback, useEffect, useId, useState } from "react";
import {
  desktopNotificationsEnabled,
  isDesktopRuntime,
  setDesktopNotifications,
  testDesktopNotification,
} from "../desktopRuntime";
import {
  createPhonePairing,
  disableWebPush,
  enableWebPush,
  loadNotificationDevices,
  removeNotificationDevice,
  testNotificationDevice,
  webPushSupported,
  type NotificationDevice,
  type PhonePairing,
} from "../notificationDevices";

const PAIRING_POLL_MS = 3000;

export function notificationStatusLabel(device: NotificationDevice | null): string {
  if (!device) return "Notifications off";
  return device.status === "delivery_failed" ? "Delivery failed" : "Notifications on";
}

function errorText(failure: unknown): string {
  return failure instanceof Error ? failure.message : String(failure);
}

/**
 * On, Off, and Test for the device in hand. The Mac app posts through macOS;
 * a browser subscribes to Web Push. Either turns on only from this tap.
 */
export function DeviceNotificationControl({
  device,
  onChange,
}: {
  device: NotificationDevice | null;
  onChange: () => void;
}) {
  const desktop = isDesktopRuntime();
  const [desktopOn, setDesktopOn] = useState<boolean | null>(desktop ? null : false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!desktop) return;
    void desktopNotificationsEnabled()
      .then(setDesktopOn)
      .catch(() => setDesktopOn(false));
  }, [desktop]);

  const run = async (action: () => Promise<string | null>) => {
    setBusy(true);
    setMessage(null);
    try {
      setMessage(await action());
    } catch (failure) {
      setMessage(errorText(failure));
    } finally {
      setBusy(false);
      onChange();
    }
  };

  if (!desktop && !webPushSupported()) {
    return (
      <span className="device-notification-note">
        To get notifications on a phone, add RCP to its Home Screen and open it from there.
      </span>
    );
  }
  const on = desktop ? desktopOn === true : device !== null;
  return (
    <span className="device-notification-control">
      <span>
        {desktop
          ? on
            ? "Notifications on"
            : "Notifications off"
          : notificationStatusLabel(device)}
      </span>
      <button
        type="button"
        disabled={busy || (desktop && desktopOn === null)}
        onClick={() =>
          void run(async () => {
            if (desktop) {
              await setDesktopNotifications(!on);
              setDesktopOn(!on);
            } else if (on) {
              await disableWebPush(device);
            } else {
              await enableWebPush();
            }
            return null;
          })
        }
      >
        {on ? "Turn off" : "Turn on"}
      </button>
      {on && (
        <button
          type="button"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              const outcome = desktop
                ? await testDesktopNotification()
                : device
                  ? (await testNotificationDevice(device.device_id)).outcome
                  : "failed";
              return outcome === "posted" ? "Test sent." : "The test could not be delivered.";
            })
          }
        >
          Test
        </button>
      )}
      {message && <span role="status">{message}</span>}
    </span>
  );
}

/**
 * A personal space has no sign-in, so its rows are notification targets: this
 * Mac and any notify-only phones. Phones have Remove, never Revoke.
 */
export function PersonalDevicesPanel({ active = true }: { active?: boolean }) {
  const [devices, setDevices] = useState<NotificationDevice[]>([]);
  const [pairing, setPairing] = useState<PhonePairing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const titleId = useId();

  const refresh = useCallback(() => {
    void loadNotificationDevices()
      .then(setDevices)
      .catch(() => setError("Devices could not be refreshed. Try again."));
  }, []);

  useEffect(() => {
    if (active) refresh();
    else setPairing(null);
  }, [active, refresh]);

  // While a code is on screen, watch for the phone to appear.
  useEffect(() => {
    if (!active || !pairing) return;
    const timer = window.setInterval(refresh, PAIRING_POLL_MS);
    return () => window.clearInterval(timer);
  }, [active, pairing, refresh]);

  const phones = devices.filter((device) => device.kind === "web_push");
  const mac = devices.find((device) => device.kind === "desktop") ?? null;

  const connectPhone = async () => {
    setBusy(true);
    setError(null);
    try {
      setPairing(await createPhonePairing());
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (deviceId: string) => {
    setError(null);
    try {
      await removeNotificationDevice(deviceId);
    } catch (failure) {
      setError(errorText(failure));
    }
    refresh();
  };

  return (
    <section
      className="landing-team-devices personal-devices"
      data-devices="personal"
      aria-labelledby={titleId}
    >
      <header>
        <span id={titleId}>Devices</span>
      </header>
      <ul>
        {isDesktopRuntime() && (
          <li>
            <div>
              <strong>This Mac</strong>
            </div>
            <DeviceNotificationControl device={mac} onChange={refresh} />
          </li>
        )}
        {phones.map((phone) => (
          <li key={phone.device_id}>
            <div>
              <strong>
                <Smartphone size={12} aria-hidden="true" /> Phone
              </strong>
              <time dateTime={phone.created_at}>
                Paired {new Date(phone.created_at).toLocaleString()}
              </time>
              <span>{notificationStatusLabel(phone)}</span>
            </div>
            <button type="button" onClick={() => void remove(phone.device_id)}>
              Remove
            </button>
          </li>
        ))}
      </ul>
      <button
        className="landing-team-connect-device"
        type="button"
        disabled={busy}
        onClick={() => void connectPhone()}
      >
        <Bell size={13} aria-hidden="true" />
        {busy ? "Issuing code" : "Connect a phone"}
      </button>
      {pairing && <PhonePairingCard pairing={pairing} onDismiss={() => setPairing(null)} />}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}

function PhonePairingCard({
  pairing,
  onDismiss,
}: {
  pairing: PhonePairing;
  onDismiss: () => void;
}) {
  const command = `tailscale serve --bg ${pairing.listener_port}`;
  return (
    <div className="landing-team-pairing" aria-live="polite">
      <p>
        The phone gets a short notice when something needs you; you open the item on this Mac.
        Notices arrive only while this Mac is awake with RCP running.
      </p>
      <p>
        1. Give this Mac an HTTPS address for the phone, for example by running{" "}
        <code>{command}</code> in Terminal.
      </p>
      <p>
        2. On the phone, open that address in Safari, tap Share, then Add to Home Screen. Open RCP
        from the Home Screen.
      </p>
      <p>3. Enter this code there.</p>
      <code tabIndex={0} aria-label={`Phone code ${pairing.code}`}>
        {pairing.code}
      </code>
      <div>
        <span>Expires {new Date(pairing.expires_at).toLocaleTimeString()}</span>
        <button type="button" onClick={onDismiss}>
          Done
        </button>
      </div>
    </div>
  );
}
