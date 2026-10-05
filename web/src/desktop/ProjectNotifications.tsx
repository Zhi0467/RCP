import { Bell } from "lucide-react";
import { useEffect, useState } from "react";

export type NotificationKind =
  | "proposal"
  | "decision"
  | "blocker"
  | "episode_needs_action"
  | "episode_finished"
  | "consolidation";

export type NotificationPreferences = Record<NotificationKind, boolean>;

export const NOTIFICATION_KINDS: Array<{ kind: NotificationKind; label: string }> = [
  { kind: "proposal", label: "Proposals" },
  { kind: "decision", label: "Decisions" },
  { kind: "blocker", label: "Blockers" },
  { kind: "episode_needs_action", label: "Episodes that need you" },
  { kind: "episode_finished", label: "Episodes finished" },
  { kind: "consolidation", label: "Nightly consolidation" },
];

interface Props {
  projectId: string;
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
}

/**
 * The calling member's notification toggles for one project. They are
 * member-owned, so a project's write fence never disables them.
 */
export function ProjectNotifications({ projectId, api }: Props) {
  const [preferences, setPreferences] = useState<NotificationPreferences | null>(null);
  const [error, setError] = useState<string | null>(null);
  const path = `/api/projects/${encodeURIComponent(projectId)}/notifications`;

  useEffect(() => {
    let current = true;
    api<NotificationPreferences>(path)
      .then((loaded) => current && setPreferences(loaded))
      .catch(
        (failure) =>
          current && setError(failure instanceof Error ? failure.message : String(failure)),
      );
    return () => {
      current = false;
    };
  }, [api, path]);

  const toggle = async (kind: NotificationKind, enabled: boolean) => {
    setError(null);
    try {
      setPreferences(
        await api<NotificationPreferences>(path, {
          method: "PATCH",
          body: JSON.stringify({ [kind]: enabled }),
        }),
      );
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  };

  return (
    <section className="settings-section project-notifications">
      <header>
        <span>
          <Bell size={16} />
        </span>
        <h2>Notifications</h2>
      </header>
      <div className="project-notification-kinds" role="group" aria-label="Notify me about">
        {NOTIFICATION_KINDS.map(({ kind, label }) => (
          <label key={kind}>
            <input
              type="checkbox"
              checked={preferences?.[kind] ?? false}
              disabled={preferences === null}
              onChange={(event) => void toggle(kind, event.target.checked)}
            />
            {label}
          </label>
        ))}
      </div>
      <p className="project-notification-note">
        Notifications show this project's name on your lock screen, never item text. Turn a device
        on under Devices.
      </p>
      {error ? <p className="project-member-error">{error}</p> : null}
    </section>
  );
}
