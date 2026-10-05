import { useMachinePower } from "../hooks/useMachinePower";
import { machinePowerWarnings } from "../machinePower";
import { ProviderLoginNotice } from "../components/ProviderLoginNotice";
import { loadProviderLogins } from "../api";
import {
  Check,
  Copy,
  Ellipsis,
  LogOut,
  Mail,
  Server,
  Settings,
  Trash2,
  TriangleAlert,
  WifiOff,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { SpaceRuns } from "../components/SpaceRuns";
import type { AppearancePickerProps, TextScaleControl } from "../components/AppearancePicker";
import type { ArchiveEpisodeAction } from "../components/EpisodeRunControls";
import { LandingIdentityMenu } from "../components/LandingIdentityMenu";
import { ProjectDock } from "../components/ProjectDock";
import { TeamSpaceGroups } from "../components/TeamSpaceGroups";
import { isDesktopRuntime } from "../desktopRuntime";
import type { ProjectTab } from "../projectTabs";
import type { ResolvedTheme } from "../theme";
import type {
  MachinePowerStatus,
  ProviderLoginState,
  IdentityResponse,
  ProjectCard,
  ProjectCreationControl,
  ProjectInvitation,
  SpaceRunIndexEntry,
} from "../types";
import { projectCreationPrimaryLabel } from "../projectSetup";

interface Props extends AppearancePickerProps {
  palette: ResolvedTheme;
  projects: ProjectCard[];
  invitations: ProjectInvitation[];
  onAnswerInvitation: (invitationId: string, response: "accept" | "decline") => Promise<void>;
  spaceRuns: SpaceRunIndexEntry[];
  onOpen: (projectId: string) => void;
  onOpenExperiment: (projectId: string, experimentRoute?: string) => void;
  onArchiveEpisode: ArchiveEpisodeAction;
  onCreate: () => void;
  projectCreation: ProjectCreationControl;
  onMovePersonalProjectToTeam?: (projectId: string) => void;
  onDelete: (projectId: string) => Promise<void> | void;
  openProjectTabs: ProjectTab[];
  onActivateProjectTab: (projectId: string) => void;
  onCloseProjectTab: (projectId: string) => void;
  identity: IdentityResponse | null;
  identityError: string | null;
  onRequestIdentityName: () => Promise<boolean> | void;
  onExitTeamSpace?: () => void;
  onOpenSpaceSettings?: () => void;
  voiceControl?: ReactNode;
  textScale?: TextScaleControl;
}

const COVER_STYLES = ["plain", "dye", "mosaic", "wood", "marble", "diffusion"] as const;
type CoverStyle = (typeof COVER_STYLES)[number];

const COVER_LABELS: Record<CoverStyle, string> = {
  plain: "Smooth",
  dye: "Linen",
  mosaic: "Crosshatch",
  wood: "Buckram",
  marble: "Laid paper",
  diffusion: "Pulp",
};

interface ProjectActionsMenuProps {
  project: ProjectCard;
  cover: CoverStyle;
  onChooseCover: (cover: CoverStyle) => void;
  onMoveToTeam?: () => void;
  onDelete: () => void;
}

export function ProjectActionsMenu({
  project,
  cover,
  onChooseCover,
  onMoveToTeam,
  onDelete,
}: ProjectActionsMenuProps) {
  return (
    <div className="project-cover-menu" role="menu" aria-label={`Actions for ${project.name}`}>
      <span className="project-cover-menu-label" role="presentation">
        Cover
      </span>
      <div className="project-cover-options" role="presentation">
        {COVER_STYLES.map((style) => (
          <button
            className="project-cover-option"
            type="button"
            role="menuitemradio"
            key={style}
            aria-checked={cover === style}
            onClick={() => onChooseCover(style)}
          >
            <span className="project-cover-swatch">
              <span
                className={`project-cover-swatch-zoom project-material-${style}`}
                aria-hidden="true"
              />
            </span>
            <span className="project-cover-option-label">{COVER_LABELS[style]}</span>
          </button>
        ))}
      </div>
      {onMoveToTeam && (
        <button
          className="project-cover-menu-action project-move-action"
          type="button"
          role="menuitem"
          onClick={onMoveToTeam}
        >
          <Server size={14} aria-hidden="true" />
          Move to team space
        </button>
      )}
      {project.can_delete && (
        <button
          className="project-cover-menu-action project-delete-action"
          type="button"
          role="menuitem"
          onClick={onDelete}
        >
          <Trash2 size={14} aria-hidden="true" />
          Delete project
        </button>
      )}
    </div>
  );
}

interface ProjectDeleteDialogProps {
  project: ProjectCard;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
}

export function ProjectDeleteDialog({
  project,
  busy,
  error,
  onClose,
  onConfirm,
}: ProjectDeleteDialogProps) {
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        className="project-delete-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="project-delete-title"
        aria-describedby="project-delete-warning"
      >
        <header>
          <Trash2 size={20} aria-hidden="true" />
          <h2 id="project-delete-title">Delete {project.name}?</h2>
        </header>
        <p id="project-delete-warning">{project.delete_confirmation}</p>
        {error && (
          <div className="project-delete-error" role="alert">
            {error}
          </div>
        )}
        <footer>
          <button
            className="button secondary"
            type="button"
            autoFocus
            disabled={busy}
            onClick={onClose}
          >
            Cancel
          </button>
          <button className="button danger" type="button" disabled={busy} onClick={onConfirm}>
            {busy ? "Deleting…" : "Delete project"}
          </button>
        </footer>
      </section>
    </div>
  );
}

export function ProjectLanding({
  projects,
  invitations,
  onAnswerInvitation,
  spaceRuns,
  onOpen,
  onOpenExperiment,
  onArchiveEpisode,
  onCreate,
  projectCreation,
  onMovePersonalProjectToTeam,
  onDelete,
  openProjectTabs,
  onActivateProjectTab,
  onCloseProjectTab,
  identity,
  identityError,
  onRequestIdentityName,
  onExitTeamSpace,
  onOpenSpaceSettings,
  voiceControl,
  textScale,
  themeChoice,
  colorModeChoice,
  onThemeChoiceChange,
  onColorModeChoiceChange,
  palette,
}: Props) {
  const machinePower = useMachinePower(identity?.space_kind);
  const [providerLogins, setProviderLogins] = useState<ProviderLoginState[]>([]);
  const [loginError, setLoginError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    loadProviderLogins().then(
      (states) => {
        if (active) {
          setProviderLogins(states);
          setLoginError(null);
        }
      },
      (error) => {
        if (active) setLoginError(String(error));
      },
    );
    return () => {
      active = false;
    };
  }, [spaceRuns]);
  const desktop = isDesktopRuntime();
  const [covers, setCovers] = useState<Record<string, CoverStyle>>(() => readCoverPreferences());
  const [openMenuProject, setOpenMenuProject] = useState<string | null>(null);
  const [deleteProjectId, setDeleteProjectId] = useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [addTeamOpen, setAddTeamOpen] = useState(false);

  useEffect(() => {
    try {
      localStorage.setItem("rcp:project-covers", JSON.stringify(covers));
    } catch {
      // Cover choices are a convenience; storage failures must not affect the project list.
    }
  }, [covers]);

  useEffect(() => {
    const closeOnPointerDown = (event: PointerEvent) => {
      if (!(event.target instanceof Element) || event.target.closest(".project-cover-shell"))
        return;
      setOpenMenuProject(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpenMenuProject(null);
      if (!deleteBusy) {
        setDeleteProjectId(null);
        setDeleteError(null);
      }
    };
    document.addEventListener("pointerdown", closeOnPointerDown);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnPointerDown);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [deleteBusy]);

  const deleteProject =
    projects.find((project) => project.id === deleteProjectId && project.can_delete) ?? null;

  const closeDeleteConfirmation = () => {
    if (deleteBusy) return;
    setDeleteProjectId(null);
    setDeleteError(null);
  };

  const confirmDelete = async () => {
    if (!deleteProject || deleteBusy) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await onDelete(deleteProject.id);
      setCovers((current) => {
        const next = { ...current };
        delete next[deleteProject.id];
        return next;
      });
      setDeleteProjectId(null);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : String(error));
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <div className="landing-shell">
      <header className="landing-header">
        <a className="rcp-mark" href="#" aria-label="RCP project index">
          <span className="rcp-wordmark" aria-hidden="true">
            RCP
          </span>
        </a>
        <ProjectDock
          tabs={openProjectTabs}
          activeProjectId={null}
          onActivate={onActivateProjectTab}
          onClose={onCloseProjectTab}
        />
        {voiceControl}
        {onOpenSpaceSettings && (
          <button
            className="landing-space-settings"
            type="button"
            aria-label="Space settings"
            title="Space settings"
            onClick={onOpenSpaceSettings}
          >
            <Settings size={16} aria-hidden="true" />
          </button>
        )}
        <LandingIdentityMenu
          identity={identity}
          identityError={identityError}
          onRequestName={onRequestIdentityName}
          onAddTeamSpace={desktop ? () => setAddTeamOpen(true) : undefined}
          appearance={{
            themeChoice,
            colorModeChoice,
            onThemeChoiceChange,
            onColorModeChoiceChange,
          }}
          textScale={textScale}
        />
      </header>

      <main className="landing-main">
        <MachinePowerWarning status={machinePower.status} onOpenSettings={onOpenSpaceSettings} />
        {machinePower.error && <p role="alert">{machinePower.error}</p>}
        <ProviderLoginNotice states={providerLogins} />
        {loginError && <p role="alert">{loginError}</p>}
        {identity?.space_kind === "personal" && (
          <h1 className="space-group-title">Personal space</h1>
        )}
        {identity?.space_kind === "team" && (
          // This index belongs to the team server. Say so, and put the one way
          // back to the local index here, so returning to projects can keep
          // meaning "this space's projects".
          <div className="space-group-title-row">
            <h1 className="space-group-title">{identity.space_name || "Team space"}</h1>
            {onExitTeamSpace && (
              <button className="team-space-exit" type="button" onClick={onExitTeamSpace}>
                <LogOut size={14} aria-hidden="true" />
                Exit team space
              </button>
            )}
          </div>
        )}
        <section className="project-shelf" aria-label="RCP projects">
          {invitations.map((invitation) => (
            <ProjectInvitationCard
              key={invitation.invitation_id}
              invitation={invitation}
              onAnswer={onAnswerInvitation}
            />
          ))}
          {projects.map((project) => {
            const unavailable = project.reachable === false;
            const cover = covers[project.id] || "wood";
            return (
              <div className="project-cover-shell" key={project.id}>
                <button
                  className={`project-cover project-material-${cover}`}
                  onClick={() => onOpen(project.id)}
                >
                  <span className="project-cover-spine" aria-hidden="true" />
                  <span
                    className={unavailable ? "project-cover-state offline" : "project-cover-state"}
                  >
                    {unavailable ? <WifiOff size={12} /> : <Server size={12} />}
                    {unavailable ? "Cached" : project.remote ? "Remote" : "Local"}
                  </span>
                  <strong>{project.name}</strong>
                  <span className="project-cover-meta">
                    {project.revision == null ? "Not opened" : `Revision ${project.revision}`}
                    {project.attention_count > 0 && <> · {project.attention_count} waiting</>}
                    {project.digest_count > 0 && (
                      <span className="project-cover-new"> · {project.digest_count} new</span>
                    )}
                    {project.last_opened_at && <> · {formatReturn(project.last_opened_at)}</>}
                  </span>
                  <span className="project-cover-open" aria-hidden="true">
                    Open →
                  </span>
                </button>
                <button
                  className="project-cover-trigger"
                  type="button"
                  aria-haspopup="menu"
                  aria-expanded={openMenuProject === project.id}
                  aria-label={`Project actions for ${project.name}`}
                  title="Project actions"
                  onClick={() =>
                    setOpenMenuProject((current) => (current === project.id ? null : project.id))
                  }
                >
                  <Ellipsis size={14} aria-hidden="true" />
                </button>
                {openMenuProject === project.id && (
                  <ProjectActionsMenu
                    project={project}
                    cover={cover}
                    onChooseCover={(style) => {
                      setCovers((current) => ({ ...current, [project.id]: style }));
                      setOpenMenuProject(null);
                    }}
                    onMoveToTeam={
                      onMovePersonalProjectToTeam
                        ? () => {
                            setOpenMenuProject(null);
                            onMovePersonalProjectToTeam(project.id);
                          }
                        : undefined
                    }
                    onDelete={() => {
                      setOpenMenuProject(null);
                      setDeleteError(null);
                      setDeleteProjectId(project.id);
                    }}
                  />
                )}
              </div>
            );
          })}

          <button className="project-cover new-project-cover" onClick={onCreate}>
            <span className="new-project-plus" aria-hidden="true">
              +
            </span>
            <strong>{projectCreationPrimaryLabel(projectCreation)}</strong>
            <span className="project-cover-open" aria-hidden="true">
              Create →
            </span>
          </button>
        </section>

        {identity?.space_kind === "personal" && (
          <TeamSpaceGroups
            addOpen={addTeamOpen}
            onOpenAdd={() => setAddTeamOpen(true)}
            onCloseAdd={() => setAddTeamOpen(false)}
          />
        )}

        <SpaceRuns
          entries={spaceRuns}
          theme={palette}
          onOpen={onOpenExperiment}
          onArchive={onArchiveEpisode}
        />
      </main>

      {deleteProject && (
        <ProjectDeleteDialog
          project={deleteProject}
          busy={deleteBusy}
          error={deleteError}
          onClose={closeDeleteConfirmation}
          onConfirm={confirmDelete}
        />
      )}
    </div>
  );
}

function ProjectInvitationCard({
  invitation,
  onAnswer,
}: {
  invitation: ProjectInvitation;
  onAnswer: (invitationId: string, response: "accept" | "decline") => Promise<void>;
}) {
  const [busy, setBusy] = useState<"accept" | "decline" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const answer = async (response: "accept" | "decline") => {
    setBusy(response);
    setError(null);
    try {
      await onAnswer(invitation.invitation_id, response);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
      setBusy(null);
    }
  };
  const inviter = invitation.invited_by_name || invitation.invited_by;
  return (
    <div className="project-cover-shell project-invitation-shell">
      <article className="project-cover project-invitation">
        <span className="project-cover-spine" aria-hidden="true" />
        <span className="project-cover-state">
          <Mail size={12} />
          Invitation
        </span>
        <strong>{invitation.project_name}</strong>
        <span className="project-cover-meta">
          {invitation.space_name ? `${invitation.space_name} · ` : ""}
          {inviter}
        </span>
        {error ? <p className="project-invitation-error">{error}</p> : null}
        <span className="project-invitation-actions">
          <button type="button" disabled={busy !== null} onClick={() => answer("accept")}>
            {busy === "accept" ? "Accepting…" : "Accept"}
          </button>
          <button type="button" disabled={busy !== null} onClick={() => answer("decline")}>
            {busy === "decline" ? "Declining…" : "Decline"}
          </button>
        </span>
      </article>
    </div>
  );
}

function formatReturn(timestamp?: string | null): string {
  if (!timestamp) return "";
  return new Date(timestamp).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

function readCoverPreferences(): Record<string, CoverStyle> {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem("rcp:project-covers") || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const covers: Record<string, CoverStyle> = {};
    for (const [projectId, style] of Object.entries(parsed)) {
      if (isCoverStyle(style)) covers[projectId] = style;
    }
    return covers;
  } catch {
    return {};
  }
}

function isCoverStyle(value: unknown): value is CoverStyle {
  return typeof value === "string" && (COVER_STYLES as readonly string[]).includes(value);
}

function MachinePowerWarning({
  status,
  onOpenSettings,
}: {
  status: MachinePowerStatus | null;
  onOpenSettings?: () => void;
}) {
  const { latch, cleanup } = machinePowerWarnings(status);
  const [copied, setCopied] = useState<string | null>(null);
  const [copyError, setCopyError] = useState(false);
  if (!latch && !cleanup) return null;
  return (
    <aside className="machine-power-warning" role="alert">
      <TriangleAlert size={20} aria-hidden="true" />
      {latch && (
        <span>
          Lid-closed mode is latched off after {latch.replaceAll("_", " ")}. Re-enable it in
          Settings.
        </span>
      )}
      {latch && onOpenSettings && (
        <button className="button secondary compact" type="button" onClick={onOpenSettings}>
          Settings
        </button>
      )}
      {cleanup && (
        <>
          <span>Keep-awake cleanup failed: {cleanup.kind.replaceAll("_", " ")}.</span>
          <code>{cleanup.command}</code>
          <button
            className="button secondary compact"
            type="button"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(cleanup.command);
                setCopied(cleanup.command);
                setCopyError(false);
              } catch {
                setCopyError(true);
              }
            }}
          >
            {copied === cleanup.command ? <Check size={14} /> : <Copy size={14} />} Copy command
          </button>
          {copyError && <span>Could not copy command</span>}
        </>
      )}
    </aside>
  );
}
