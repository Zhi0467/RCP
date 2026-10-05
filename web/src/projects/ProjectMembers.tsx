import { Users } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { IdentityResponse, ProjectMember, SpaceUserSummary } from "../types";

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
  const [members, setMembers] = useState<ProjectMember[] | null>(null);
  const [spaceUsers, setSpaceUsers] = useState<SpaceUserSummary[] | null>(null);
  const [candidates, setCandidates] = useState<SpaceUserSummary[]>([]);
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invited, setInvited] = useState<string | null>(null);

  const base = `/api/projects/${encodeURIComponent(projectId)}`;

  const reload = useCallback(async () => {
    const [seated, everyone] = await Promise.all([
      api<ProjectMember[]>(`${base}/members`),
      api<SpaceUserSummary[]>("/api/space/users"),
    ]);
    setMembers(seated);
    setSpaceUsers(everyone);
    const seatedIds = new Set(seated.map((member) => member.user_id));
    setCandidates(everyone.filter((user) => !seatedIds.has(user.user_id)));
  }, [api, base]);

  useEffect(() => {
    void reload().catch((failure) =>
      setError(failure instanceof Error ? failure.message : String(failure)),
    );
  }, [reload]);

  const invite = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    setInvited(null);
    try {
      await api(`${base}/invitations`, {
        method: "POST",
        body: JSON.stringify({ user_id: selected }),
      });
      const name = candidates.find((user) => user.user_id === selected)?.display_name;
      setInvited(name || selected);
      setSelected("");
      await reload();
    } catch (failure) {
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
    <section className="settings-section project-members">
      <header>
        <span>
          <Users size={16} />
        </span>
        <h2>Members</h2>
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

      {error ? <p className="project-member-error">{error}</p> : null}
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
