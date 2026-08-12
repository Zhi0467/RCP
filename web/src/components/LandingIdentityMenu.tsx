import {
  Check,
  ChevronDown,
  Copy,
  Link2,
  MailPlus,
  Pencil,
  UserPlus,
  UserRound,
} from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import type { IdentityResponse } from "../types";

interface Props {
  identity: IdentityResponse | null;
  identityError: string | null;
  onRequestName: () => Promise<boolean> | void;
}

interface IdentityProvenanceSlipProps {
  identity: IdentityResponse;
  identityError: string | null;
  teamNoticeId: string;
  copyStatus: "idle" | "copied" | "failed";
  onCopy: () => void;
  onEdit: () => void;
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

      <section
        className="landing-team-seam"
        aria-labelledby={`${teamNoticeId}-title`}
        data-team-space-seam="unimplemented"
      >
        <header>
          <span id={`${teamNoticeId}-title`}>Team spaces</span>
          <span>Coming later</span>
        </header>
        <p id={teamNoticeId}>Team connections are not implemented in this build.</p>
        <div className="landing-team-seam-actions">
          <button type="button" disabled aria-describedby={teamNoticeId}>
            <Link2 size={13} aria-hidden="true" />
            Join team space
          </button>
          <button type="button" disabled aria-describedby={teamNoticeId}>
            <MailPlus size={13} aria-hidden="true" />
            Accept invitation
          </button>
          <button type="button" disabled aria-describedby={teamNoticeId}>
            <UserPlus size={13} aria-hidden="true" />
            Invite member
          </button>
        </div>
      </section>
    </>
  );
}

export function LandingIdentityMenu({ identity, identityError, onRequestName }: Props) {
  const [open, setOpen] = useState(false);
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "failed">("idle");
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
          />
        </section>
      )}
    </div>
  );
}

function identityInitial(displayName: string): string {
  return Array.from(displayName.trim())[0]?.toLocaleUpperCase() ?? "?";
}
