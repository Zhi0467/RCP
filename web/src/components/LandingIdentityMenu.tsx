import {
  Check,
  ChevronDown,
  Copy,
  Link2,
  Pencil,
  RefreshCw,
  Smartphone,
  UserPlus,
  UserRound,
} from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import {
  createTeamDevicePairing,
  createTeamInvitation,
  loadTeamDevicePairing,
  loadSpaceUsers,
  loadTeamInvitations,
  loadTeamSessions,
  revokeTeamInvitation,
  revokeTeamSession,
} from "../api";
import { listDesktopTeamConnections, type TeamConnectionMetadata } from "../desktopRuntime";
import type {
  IdentityResponse,
  SpaceUserSummary,
  TeamDevicePairing,
  TeamDevicePairingState,
  TeamInvitation,
  TeamInvitationIssue,
  TeamSession,
} from "../types";

interface Props {
  identity: IdentityResponse | null;
  identityError: string | null;
  onRequestName: () => Promise<boolean> | void;
  onAddTeamSpace?: () => void;
}

interface IdentityProvenanceSlipProps {
  identity: IdentityResponse;
  identityError: string | null;
  teamNoticeId: string;
  copyStatus: "idle" | "copied" | "failed";
  onCopy: () => void;
  onEdit: () => void;
  teamPanelActive?: boolean;
  onAddTeamSpace?: () => void;
  teamSpaces?: TeamConnectionMetadata[];
}

export async function copyIdentityId(
  userId: string,
  clipboard: Pick<Clipboard, "writeText"> | undefined = typeof navigator === "undefined"
    ? undefined
    : navigator.clipboard,
): Promise<void> {
  if (!clipboard) throw new Error("Clipboard access is unavailable.");
  await clipboard.writeText(userId);
}

export function IdentityProvenanceSlip({
  identity,
  identityError,
  teamNoticeId,
  copyStatus,
  onCopy,
  onEdit,
  teamPanelActive = true,
  onAddTeamSpace,
  teamSpaces,
}: IdentityProvenanceSlipProps) {
  const displayName = identity.user.display_name ?? "";
  const spaceLabel = identity.space_kind === "personal" ? "Personal space" : "Team space";

  return (
    <>
      <div className="landing-identity-slip" data-identity-record="provenance-slip">
        <header>
          <span>Identity record</span>
          <span>{identity.user.identity_kind === "local_owner" ? "Local" : "Member"}</span>
        </header>
        <div className="landing-identity-slip-person">
          <span className="landing-identity-avatar large" aria-hidden="true">
            {identityInitial(displayName)}
          </span>
          <span>
            <strong>{displayName}</strong>
            <small>{spaceLabel}</small>
          </span>
          <button
            className="landing-identity-edit"
            type="button"
            data-identity-action="edit"
            onClick={onEdit}
          >
            <Pencil size={12} aria-hidden="true" />
            Edit
          </button>
        </div>
        <dl>
          <div>
            <dt>User ID</dt>
            <dd>
              <code tabIndex={0} aria-label={`User ID ${identity.user.user_id}`}>
                {identity.user.user_id}
              </code>
              <button
                className="landing-identity-copy"
                type="button"
                data-identity-action="copy-id"
                aria-label="Copy user ID"
                onClick={onCopy}
              >
                {copyStatus === "copied" ? (
                  <Check size={12} aria-hidden="true" />
                ) : (
                  <Copy size={12} aria-hidden="true" />
                )}
                {copyStatus === "copied" ? "Copied" : "Copy"}
              </button>
            </dd>
          </div>
          <div>
            <dt>Scope</dt>
            <dd>{spaceLabel}</dd>
          </div>
        </dl>
        {copyStatus === "failed" && (
          <p className="landing-identity-copy-error" role="alert">
            User ID could not be copied. Select it above to copy manually.
          </p>
        )}
        {identityError && (
          <p className="landing-identity-panel-error" role="alert">
            {identityError}
          </p>
        )}
      </div>

      {identity.space_kind === "team" ? (
        <>
          <TeamInvitationPanel identity={identity} active={teamPanelActive} />
          <TeamDevicesPanel key={identity.user.user_id} active={teamPanelActive} />
        </>
      ) : (
        <PersonalTeamSeam
          noticeId={teamNoticeId}
          onAddTeamSpace={onAddTeamSpace}
          teamSpaces={teamSpaces}
        />
      )}
    </>
  );
}

/**
 * The saved team spaces, named. Entering one stays the project index's job —
 * this roster only has to stop the record from reading as "you have none".
 * Names come from saved connection metadata, so listing them costs no tunnel.
 */
export function PersonalTeamSeam({
  noticeId,
  onAddTeamSpace,
  teamSpaces = [],
}: {
  noticeId: string;
  onAddTeamSpace?: () => void;
  teamSpaces?: TeamConnectionMetadata[];
}) {
  return (
    <section
      className="landing-team-seam"
      aria-labelledby={`${noticeId}-title`}
      data-team-space-seam="available"
    >
      <header>
        <span id={`${noticeId}-title`}>Team spaces</span>
        <span>Desktop</span>
      </header>
      {teamSpaces.length > 0 && (
        <ul className="landing-team-seam-list">
          {teamSpaces.map((connection) => (
            <li key={connection.connection_id}>{connection.display_name}</li>
          ))}
        </ul>
      )}
      <div className="landing-team-seam-actions">
        {onAddTeamSpace && (
          <button type="button" onClick={onAddTeamSpace}>
            <Link2 size={13} aria-hidden="true" />
            Add team space
          </button>
        )}
      </div>
    </section>
  );
}

const DEVICE_PAIRING_POLL_MS = 4000;
const DEVICE_PAIRING_ENDED: Record<
  Exclude<TeamDevicePairingState, "waiting" | "consumed">,
  string
> = {
  expired: "The device code expired before a device connected. Issue a new one.",
  revoked: "The device code was withdrawn. Issue a new one.",
  locked: "The device code was locked after too many wrong attempts. Issue a new one.",
};

export function TeamDevicesPanel({ active = true }: { active?: boolean }) {
  const [sessions, setSessions] = useState<TeamSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [pairing, setPairing] = useState<TeamDevicePairing | null>(null);
  const [issuing, setIssuing] = useState(false);
  const titleId = useId();

  useEffect(() => {
    if (!active) {
      // A pairing code is shown once, while the panel is open. Closing it
      // discards the code; the server still holds it until it expires.
      setPairing(null);
      return;
    }
    let stopped = false;
    setLoading(true);
    setError(null);
    void loadTeamSessions()
      .then((next) => {
        if (!stopped) setSessions(next);
      })
      .catch(() => {
        if (!stopped) setError("Devices could not be refreshed. Try again.");
      })
      .finally(() => {
        if (!stopped) setLoading(false);
      });
    return () => {
      stopped = true;
    };
  }, [active, refreshVersion]);

  // While a code is on screen, watch that code's own state, so the person
  // holding the phone sees the device appear here without pressing Refresh.
  useEffect(() => {
    if (!active || !pairing) return;
    let stopped = false;
    const timer = window.setInterval(() => {
      void loadTeamDevicePairing(pairing.pairing_id)
        .then((state) => {
          if (stopped || state.status === "waiting") return;
          setPairing(null);
          if (state.status === "consumed") {
            setRefreshVersion((current) => current + 1);
          } else {
            setError(DEVICE_PAIRING_ENDED[state.status]);
          }
        })
        .catch(() => {
          // A missed poll is not an error the person can act on; the next tick retries.
        });
    }, DEVICE_PAIRING_POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [active, pairing]);

  const revoke = async (sessionId: string) => {
    setRevoking(sessionId);
    setError(null);
    try {
      await revokeTeamSession(sessionId);
      setRefreshVersion((current) => current + 1);
    } catch {
      setError("That device could not be revoked. Refresh devices and try again.");
    } finally {
      setRevoking(null);
    }
  };

  const connectDevice = async () => {
    if (issuing) return;
    setIssuing(true);
    setError(null);
    try {
      setPairing(await createTeamDevicePairing());
    } catch {
      setError("A device code could not be issued. Try again.");
    } finally {
      setIssuing(false);
    }
  };

  return (
    <section className="landing-team-devices" aria-labelledby={titleId} aria-busy={loading}>
      <header>
        <span id={titleId}>Devices</span>
        <button
          type="button"
          disabled={loading || revoking !== null}
          onClick={() => setRefreshVersion((current) => current + 1)}
        >
          <RefreshCw size={10} aria-hidden="true" />
          Refresh
        </button>
      </header>
      <button
        className="landing-team-connect-device"
        type="button"
        disabled={issuing || loading}
        onClick={() => void connectDevice()}
      >
        <Smartphone size={13} aria-hidden="true" />
        {issuing ? "Issuing code" : "Connect a device"}
      </button>
      {pairing && <TeamDevicePairingCard pairing={pairing} onDismiss={() => setPairing(null)} />}
      {error && <p role="alert">{error}</p>}
      {loading ? (
        <p role="status">Loading devices…</p>
      ) : (
        <TeamSessionList sessions={sessions} revoking={revoking} onRevoke={revoke} />
      )}
    </section>
  );
}

export function TeamDevicePairingCard({
  pairing,
  onDismiss,
}: {
  pairing: TeamDevicePairing;
  onDismiss: () => void;
}) {
  return (
    <div className="landing-team-pairing" aria-live="polite">
      <p>
        On the other device, open this team space in its browser, choose{" "}
        <strong>Connect this device</strong>, and enter this code with a name for the device.
      </p>
      <code tabIndex={0} aria-label={`Device code ${pairing.code}`}>
        {pairing.code}
      </code>
      <div>
        <span>Expires {formatInvitationTime(pairing.expires_at)}</span>
        <button type="button" onClick={onDismiss}>
          Done
        </button>
      </div>
    </div>
  );
}

export function TeamSessionList({
  sessions,
  revoking,
  onRevoke,
}: {
  sessions: TeamSession[];
  revoking: string | null;
  onRevoke: (sessionId: string) => void | Promise<void>;
}) {
  return (
    <ul>
      {sessions.map((session) => (
        <li key={session.session_id}>
          <div>
            <strong>{session.label}</strong>
            {session.is_current && <strong>Current device</strong>}
            <time dateTime={session.created_at}>
              Connected {formatInvitationTime(session.created_at)}
            </time>
            <time dateTime={session.last_seen_at}>
              Last seen {formatInvitationTime(session.last_seen_at)}
            </time>
          </div>
          {session.can_revoke && (
            <button
              type="button"
              disabled={revoking !== null}
              onClick={() => void onRevoke(session.session_id)}
              aria-label={`Revoke device connected ${formatInvitationTime(session.created_at)}`}
            >
              {revoking === session.session_id ? "Revoking…" : "Revoke"}
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

export function TeamInvitationPanel({
  identity,
  active = true,
}: {
  identity: IdentityResponse;
  active?: boolean;
}) {
  const [invitations, setInvitations] = useState<TeamInvitation[]>([]);
  const [members, setMembers] = useState<SpaceUserSummary[] | null>(null);
  const [issued, setIssued] = useState<TeamInvitationIssue | null>(null);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "failed">("idle");
  const titleId = useId();

  useEffect(() => {
    if (!active) {
      // A raw invitation code is shown once, at the moment it is created.
      // Closing the panel discards it, so reopening shows only metadata.
      setIssued(null);
      setCopyStatus("idle");
      return;
    }
    let stopped = false;
    setLoading(true);
    setError(null);
    void Promise.all([loadTeamInvitations(), loadSpaceUsers()])
      .then(([nextInvitations, nextMembers]) => {
        if (!stopped) {
          setInvitations(nextInvitations);
          setMembers(nextMembers);
        }
      })
      .catch(() => {
        if (!stopped) setError("Team members and invitations could not be refreshed.");
      })
      .finally(() => {
        if (!stopped) setLoading(false);
      });
    return () => {
      stopped = true;
    };
  }, [active, identity.user.user_id, refreshVersion]);

  const createInvitation = async () => {
    if (creating) return;
    setCreating(true);
    setError(null);
    setCopyStatus("idle");
    try {
      const next = await createTeamInvitation();
      setIssued(next);
      setInvitations((current) => [
        next.invitation,
        ...current.filter(
          (invitation) => invitation.invitation_id !== next.invitation.invitation_id,
        ),
      ]);
    } catch {
      setError("An invitation could not be created. Try again.");
    } finally {
      setCreating(false);
    }
  };

  const revokeInvitation = async (invitationId: string) => {
    setError(null);
    try {
      const revoked = await revokeTeamInvitation(invitationId);
      setInvitations((current) =>
        current.map((invitation) =>
          invitation.invitation_id === revoked.invitation_id ? revoked : invitation,
        ),
      );
      // The panel still holds the raw code it just showed; drop it so a
      // revoked code cannot be copied out of a stale card.
      setIssued((current) => (current?.invitation.invitation_id === invitationId ? null : current));
    } catch {
      setError("That invitation could not be revoked. Try opening this panel again.");
    }
  };

  const copyInvitation = async () => {
    if (!issued) return;
    try {
      await copyIdentityId(invitationCopyBlock(issued));
      setCopyStatus("copied");
    } catch {
      setCopyStatus("failed");
    }
  };

  return (
    <>
      {members && (
        <TeamMemberRoster
          members={members}
          currentUserId={identity.user.user_id}
          loading={loading}
        />
      )}
      <section className="landing-team-invitations" aria-labelledby={titleId}>
        <header>
          <span id={titleId}>Team invitations</span>
          <span className="landing-team-invitation-tools">
            <span>{identity.space_name || "Team space"}</span>
            <button
              type="button"
              disabled={loading}
              onClick={() => setRefreshVersion((current) => current + 1)}
            >
              <RefreshCw size={10} aria-hidden="true" />
              Refresh
            </button>
          </span>
        </header>
        <button
          className="landing-team-invite-action"
          type="button"
          disabled={creating}
          onClick={() => void createInvitation()}
        >
          <UserPlus size={13} aria-hidden="true" />
          {creating ? "Creating" : "Invite member"}
        </button>

        {issued && (
          <div className="landing-team-invitation-code" aria-live="polite">
            <div>
              <span>Invitation for</span>
              <strong>{issued.space_name}</strong>
            </div>
            <code tabIndex={0} aria-label={`Invitation code ${issued.code}`}>
              {issued.code}
            </code>
            <div className="landing-team-invitation-expiry">
              Expires {formatInvitationTime(issued.invitation.expires_at)}
            </div>
            <button type="button" onClick={() => void copyInvitation()}>
              {copyStatus === "copied" ? (
                <Check size={12} aria-hidden="true" />
              ) : (
                <Copy size={12} aria-hidden="true" />
              )}
              {copyStatus === "copied" ? "Copied" : "Copy invitation"}
            </button>
            {copyStatus === "failed" && (
              <p role="alert">
                Invitation could not be copied. Select the code to copy it manually.
              </p>
            )}
          </div>
        )}

        {error && (
          <p className="landing-team-invitation-error" role="alert">
            {error}
          </p>
        )}

        <TeamInvitationLedger
          invitations={invitations}
          loading={loading}
          onRevoke={revokeInvitation}
        />
      </section>
    </>
  );
}

export function TeamMemberRoster({
  members,
  currentUserId,
  loading = false,
}: {
  members: SpaceUserSummary[];
  currentUserId: string;
  loading?: boolean;
}) {
  return (
    <section className="landing-team-members" aria-busy={loading}>
      <header>
        <span>Team members</span>
        <strong>{members.length}</strong>
      </header>
      <ul>
        {members.map((member) => (
          <li key={member.user_id}>
            {member.display_name || "Unnamed member"}
            {member.user_id === currentUserId ? " (you)" : ""}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function TeamInvitationLedger({
  invitations,
  loading = false,
  onRevoke,
}: {
  invitations: TeamInvitation[];
  loading?: boolean;
  onRevoke?: (invitationId: string) => void | Promise<void>;
}) {
  return (
    <div className="landing-team-invitation-ledger" aria-busy={loading}>
      <span>Created by you</span>
      {!loading && invitations.length === 0 && <p>No invitations created yet.</p>}
      {invitations.length > 0 && (
        <ul>
          {invitations.map((invitation) => (
            <li key={invitation.invitation_id}>
              <span>{invitation.status_label}</span>
              <time dateTime={invitation.expires_at}>
                Expires {formatInvitationTime(invitation.expires_at)}
              </time>
              {onRevoke && invitation.can_revoke && (
                <button
                  type="button"
                  onClick={() => void onRevoke(invitation.invitation_id)}
                  aria-label={`Revoke invitation expiring ${formatInvitationTime(
                    invitation.expires_at,
                  )}`}
                >
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function invitationCopyBlock(issue: TeamInvitationIssue): string {
  return `${issue.space_name}\n${issue.code}\nExpires ${formatInvitationTime(issue.invitation.expires_at)}`;
}

function formatInvitationTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function LandingIdentityMenu({
  identity,
  identityError,
  onRequestName,
  onAddTeamSpace,
}: Props) {
  const [open, setOpen] = useState(false);
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "failed">("idle");
  const [teamSpaces, setTeamSpaces] = useState<TeamConnectionMetadata[]>([]);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const teamNoticeId = useId();
  const displayName = identity?.user.display_name?.trim() ?? "";
  const namedIdentity = identity && displayName ? identity : null;
  const spaceLabel = identity?.space_kind === "team" ? "Team space" : "Personal space";

  useEffect(() => {
    if (namedIdentity) return;
    setOpen(false);
    setCopyStatus("idle");
  }, [namedIdentity]);

  useEffect(() => {
    if (!open) return;
    const closeOnPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && rootRef.current?.contains(event.target)) return;
      setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", closeOnPointerDown);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnPointerDown);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  const requestName = () => {
    setOpen(false);
    void onRequestName();
  };

  const copyUserId = async () => {
    if (!namedIdentity) return;
    try {
      await copyIdentityId(namedIdentity.user.user_id);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("failed");
    }
  };

  useEffect(() => {
    if (!open || identity?.space_kind !== "personal") return;
    let stopped = false;
    void listDesktopTeamConnections()
      .then((next) => {
        if (!stopped) setTeamSpaces(next);
      })
      .catch(() => {
        // The roster is a courtesy, not authority. An unreadable registry
        // leaves the section at its Add action rather than claiming none.
        if (!stopped) setTeamSpaces([]);
      });
    return () => {
      stopped = true;
    };
  }, [open, identity?.space_kind]);

  return (
    <div className={`landing-identity-menu${identityError ? " has-error" : ""}`} ref={rootRef}>
      <button
        className="landing-identity-trigger"
        type="button"
        ref={triggerRef}
        aria-haspopup={namedIdentity ? "dialog" : undefined}
        aria-expanded={namedIdentity ? open : undefined}
        aria-controls={namedIdentity ? panelId : undefined}
        onClick={() => {
          if (!namedIdentity) {
            requestName();
            return;
          }
          setCopyStatus("idle");
          setOpen((current) => !current);
        }}
      >
        <span className="landing-identity-avatar" aria-hidden="true">
          {namedIdentity ? identityInitial(displayName) : <UserRound size={14} />}
        </span>
        <span className="landing-identity-trigger-copy">
          <strong>{namedIdentity ? displayName : "Sign in"}</strong>
          {namedIdentity && <small>{spaceLabel}</small>}
        </span>
        {namedIdentity && <ChevronDown size={13} aria-hidden="true" />}
      </button>

      {identityError && (
        <span className="landing-identity-trigger-error" role="alert">
          {identityError}
        </span>
      )}

      {namedIdentity && (
        <section
          className="landing-identity-panel"
          id={panelId}
          role="dialog"
          aria-modal="false"
          aria-label="Your identity and spaces"
          hidden={!open}
        >
          <IdentityProvenanceSlip
            identity={namedIdentity}
            identityError={identityError}
            teamNoticeId={teamNoticeId}
            copyStatus={copyStatus}
            onCopy={() => void copyUserId()}
            onEdit={requestName}
            teamPanelActive={open}
            teamSpaces={teamSpaces}
            onAddTeamSpace={onAddTeamSpace}
          />
        </section>
      )}
    </div>
  );
}

function identityInitial(displayName: string): string {
  return Array.from(displayName.trim())[0]?.toLocaleUpperCase() ?? "?";
}
