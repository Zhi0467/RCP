import { Users, RotateCw } from "lucide-react";
import { useCallback, useState } from "react";
import type { IdentityResponse, SpaceUserSummary } from "../core/types";

import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime } from "../core/useResource";
import { membersProfile, useProjectResource } from "./projectResources";

interface Props {
  projectId: string;
  identity: IdentityResponse | null;
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
  onLeft: () => void;
}

/**
 * Why Invite has nobody to offer, or `null` when it does.
 *
 * The control resolves to a durable user id, so it can only ever list people
 * already enrolled in the space. Offering an empty, unexplained dropdown makes
 * a supported action look broken, so say which of the two reasons applies and
 * name the step that fixes it.
 */
export function inviteUnavailableReason(
  spaceUsers: SpaceUserSummary[] | null,
  candidates: SpaceUserSummary[],
): string | null {
  if (candidates.length > 0) return null;
  if (spaceUsers === null) return null;
  if (spaceUsers.length <= 1)
    return "You are the only person in this space. Create a team invitation from the identity menu on the project index to enrol someone, then invite them here.";
  return "Everyone in this space is already on this project.";
}

/**
 * S122 — who is on this project, one Invite control, and Leave.
 *
 * Every member has the same authority: there is no owner and no rank, so the
 * list carries names only. Leaving is refused for the last member, and that is
 * the one refusal that has to explain itself, because the control is otherwise
 * identical to every other member's.
 */
export function ProjectMembers({ projectId, identity, api, onLeft }: Props) {
  const { store } = useRefreshRuntime();
  const profile = membersProfile(projectId);
  const resource = useProjectResource(profile);
  const resourceId = resourceKeyId(profile.key);
  const members = resource.value?.members ?? null;
  const spaceUsers = resource.value?.spaceUsers ?? null;
  const seatedIds = new Set((members ?? []).map((member) => member.user_id));
  const candidates = (spaceUsers ?? []).filter((user) => !seatedIds.has(user.user_id));
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invited, setInvited] = useState<string | null>(null);

  const base = `/api/projects/${encodeURIComponent(projectId)}`;

  const reload = useCallback(async () => store.invalidate(resourceId), [store, resourceId]);

  const invite = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    setInvited(null);
    const mutation = store.beginMutation(
      crypto.randomUUID(),
      new Map([[resourceId, (value: unknown) => value]]),
    );
    try {
      await api(`${base}/invitations`, {
        method: "POST",
        body: JSON.stringify({ user_id: selected }),
      });
      const name = candidates.find((user) => user.user_id === selected)?.display_name;
      setInvited(name || selected);
      setSelected("");
      mutation.settle();
    } catch (failure) {
      mutation.fail();
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };

  const leave = async () => {
    setBusy(true);
    setError(null);
    try {
      // JSON even when empty: a team space refuses a bodyless mutation.
      await api(`${base}/leave`, { method: "POST", body: JSON.stringify({}) });
      onLeft();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
      setBusy(false);
    }
  };

  const alone = members !== null && members.length === 1;
  // A personal space holds exactly one human forever, so there is no one to
  // invite and no control to offer.
  const canInvite = identity?.space_kind === "team";
  const inviteBlocked = inviteUnavailableReason(spaceUsers, candidates);

  return (
    <section
      className="settings-section project-members"
      style={{ borderColor: error || resource.error ? "var(--danger, #d84c4c)" : undefined }}
    >
      <header>
        <span>
          <Users size={16} />
        </span>
        <h2>Members</h2>
        {error || resource.error ? (
          <button
            type="button"
            className="icon-button"
            aria-label="Retry refresh"
            title={error ?? String(resource.error)}
            onClick={() => {
              setError(null);
              void reload();
            }}
          >
            <RotateCw size={14} />
          </button>
        ) : null}
      </header>

      <ul className="project-member-list">
        {(members ?? []).map((member) => (
          <li key={member.user_id}>
            <span className="project-member-name">
              {member.display_name || "Unnamed member"}
              {member.user_id === identity?.user.user_id ? " (you)" : ""}
            </span>
          </li>
        ))}
      </ul>

      {invited ? <p className="project-member-invited">Invited {invited}.</p> : null}

      {canInvite && inviteBlocked ? <p className="project-member-note">{inviteBlocked}</p> : null}

      <div className="project-member-actions">
        {canInvite ? (
          <>
            <select
              value={selected}
              disabled={busy || candidates.length === 0}
              onChange={(event) => setSelected(event.target.value)}
              aria-label="Invite a member of this space"
            >
              <option value="">Invite member…</option>
              {candidates.map((user) => (
                <option key={user.user_id} value={user.user_id}>
                  {user.display_name || user.user_id}
                </option>
              ))}
            </select>
            <button type="button" disabled={busy || !selected} onClick={invite}>
              Invite
            </button>
          </>
        ) : null}
        <button
          type="button"
          className="project-member-leave"
          disabled={busy || alone}
          onClick={leave}
          title={alone ? "Add another member to this project before leaving." : undefined}
        >
          Leave project
        </button>
      </div>
    </section>
  );
}
