import { projectGroupShelf, parseProjectGroupHash, projectGroupHash } from "./projectGroups";
import {
  GroupNameDialog,
  useProjectGroupActions,
  useProjectGroupDrag,
} from "./ProjectGroupControls";
import { useMachinePower } from "./useMachinePower";
import { machinePowerWarnings } from "./machinePower";
import { ProviderLoginNotice } from "./ProviderLoginNotice";
import { waitsOnOtherMember } from "./providerLoginsModel";
import { api, loadProjectDeletionPreview, loadProviderLogins } from "../core/api";
import {
  Check,
  Copy,
  Ellipsis,
  FolderPlus,
  LogOut,
  Mail,
  RotateCw,
  Server,
  Settings,
  Trash2,
  TriangleAlert,
  WifiOff,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { SpaceRuns } from "../experiments/SpaceRuns";
import type { AppearancePickerProps, TextScaleControl } from "../ui/AppearancePicker";
import type { ArchiveEpisodeAction } from "../experiments/EpisodeRunControls";
import { LandingIdentityMenu } from "./LandingIdentityMenu";
import { ProjectDock } from "./ProjectDock";
import { TeamSpaceGroups } from "./TeamSpaceGroups";
import { isDesktopRuntime } from "../core/desktopRuntime";
import type { ProjectTab } from "./projectTabs";
import type { ResolvedTheme } from "../ui/theme";
import type {
  MachinePowerStatus,
  ProjectLoginNotice,
  ProviderLoginState,
  IdentityResponse,
  ProjectCard,
  ProjectGroup,
  ProjectDeletionPreview,
  ProjectDeletionResult,
  ProjectCreationControl,
  ProjectInvitation,
  SpaceRunIndexEntry,
} from "../core/types";
import { projectCreationPrimaryLabel } from "./projectSetupModel";

interface Props extends AppearancePickerProps {
  palette: ResolvedTheme;
  projects: ProjectCard[];
  groups: ProjectGroup[];
  onGroupsChanged: () => Promise<void>;
  groupFiling: { projectId: string; groupId: string } | null;
  onGroupFilingTaken: () => void;
  invitations: ProjectInvitation[];
  onAnswerInvitation: (invitationId: string, response: "accept" | "decline") => Promise<void>;
  spaceRuns: SpaceRunIndexEntry[];
  onOpen: (projectId: string) => void;
  onOpenExperiment: (projectId: string, experimentRoute?: string) => void;
  onArchiveEpisode: ArchiveEpisodeAction;
  onCreate: (groupId?: string) => void;
  projectCreation: ProjectCreationControl;
  onMovePersonalProjectToTeam?: (projectId: string) => void;
  onDelete: (projectId: string, stopToken: string) => Promise<ProjectDeletionResult>;
  openProjectTabs: ProjectTab[];
  onActivateProjectTab: (projectId: string) => void;
  onCloseProjectTab: (projectId: string) => void;
  identity: IdentityResponse | null;
  identityError: string | null;
  onRequestIdentityName: () => Promise<boolean> | void;
  onExitTeamSpace?: () => void;
  onOpenSpaceSettings?: () => void;
  onOpenSignIns?: (state: ProjectLoginNotice) => void;
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
  groups: ProjectGroup[];
  onMoveToGroup: (id: string | null) => void;
  onNewGroup: () => void;
  onMoveToTeam?: () => void;
  onDelete: () => void;
}

export function ProjectActionsMenu({
  project,
  cover,
  onChooseCover,
  groups = [],
  onMoveToGroup,
  onNewGroup,
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
      <span className="project-cover-menu-label">Move to group</span>
      {groups
        .filter((group) => group.id !== project.group_id)
        .map((group) => (
          <button
            key={group.id}
            className="project-cover-menu-action"
            role="menuitem"
            onClick={() => onMoveToGroup(group.id)}
          >
            {group.name}
          </button>
        ))}
      {project.group_id && (
        <button
          className="project-cover-menu-action"
          role="menuitem"
          onClick={() => onMoveToGroup(null)}
        >
          No group
        </button>
      )}
      <button className="project-cover-menu-action" role="menuitem" onClick={onNewGroup}>
        New group…
      </button>
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
  /** Null while loading; Delete waits for the list of work it will stop. */
  preview: ProjectDeletionPreview | "failed" | null;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
  onRetryPreview: () => void;
}

export function ProjectDeleteDialog({
  project,
  preview,
  busy,
  error,
  onClose,
  onConfirm,
  onRetryPreview,
}: ProjectDeleteDialogProps) {
  const loaded = preview !== null && preview !== "failed" ? preview : null;
  const stops = loaded?.stops ?? [];
  const blocked = loaded?.blocked_reason ?? null;
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        className={`project-delete-dialog${preview === "failed" ? " failed" : ""}`}
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
        {stops.length > 0 && (
          <div className="project-delete-stops">
            <p>Deleting stops this running work first:</p>
            <ul>
              {stops.map((group) => (
                <li key={group.summary}>
                  {group.summary}
                  {group.items.length > 0 && `: ${group.items.join(", ")}`}
                </li>
              ))}
            </ul>
            <p>Nothing they are writing is kept. No run reports are made.</p>
          </div>
        )}
        {(error ?? blocked) && (
          <div className="project-delete-error" role="alert">
            {error ?? blocked}
          </div>
        )}
        <footer>
          {preview === "failed" && (
            <button
              className="icon-button"
              type="button"
              aria-label="Retry loading the work Delete stops"
              title="Could not load the work Delete stops"
              onClick={onRetryPreview}
            >
              <RotateCw size={16} />
            </button>
          )}
          <button
            className="button secondary"
            type="button"
            autoFocus
            disabled={busy}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            className="button danger"
            type="button"
            disabled={busy || loaded === null || blocked !== null}
            onClick={onConfirm}
          >
            {busy ? "Deleting…" : stops.length > 0 ? "Stop work and delete" : "Delete project"}
          </button>
        </footer>
      </section>
    </div>
  );
}

/** Shown after Delete only when some in-flight work could not be confirmed stopped. */
export function ProjectDeletedDialog({
  name,
  unstopped,
  onClose,
}: {
  name: string;
  unstopped: string[];
  onClose: () => void;
}) {
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
        aria-labelledby="project-deleted-title"
      >
        <header>
          <TriangleAlert size={20} aria-hidden="true" />
          <h2 id="project-deleted-title">{name} deleted</h2>
        </header>
        <div className="project-delete-stops">
          <p>RCP could not stop this work. It may still be running:</p>
          <ul>
            {unstopped.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
        <footer>
          <button className="button secondary" type="button" autoFocus onClick={onClose}>
            Close
          </button>
        </footer>
      </section>
    </div>
  );
}

export function ProjectLanding({
  projects,
  groups = [],
  onGroupsChanged,
  groupFiling,
  onGroupFilingTaken,
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
  onOpenSignIns,
  voiceControl,
  textScale,
  themeChoice,
  colorModeChoice,
  onThemeChoiceChange,
  onColorModeChoiceChange,
  palette,
}: Props) {
  const [groupId, setGroupId] = useState(() =>
    parseProjectGroupHash(typeof window === "undefined" ? "" : window.location.hash),
  );
  useEffect(() => {
    const update = () => {
      setGroupId(parseProjectGroupHash(window.location.hash));
      setGroupMenu(false);
    };
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  const shelf = projectGroupShelf(projects, groups);
  const group = shelf.groups.find((item) => item.id === groupId);
  const shownProjects = group ? group.projects : shelf.ungrouped;
  const spaceTitle =
    identity?.space_kind === "team" ? identity.space_name || "Team space" : "Personal space";
  const actions = useProjectGroupActions(onGroupsChanged);
  const drag = useProjectGroupDrag(projects, groups, actions.move);
  useEffect(() => {
    if (!groupFiling) return;
    onGroupFilingTaken();
    // A failed filing is keyed to the group, so its shelf header keeps the retry.
    void actions
      .run(groupFiling.groupId, () =>
        api(`/api/projects/${encodeURIComponent(groupFiling.projectId)}/group`, {
          method: "PUT",
          body: JSON.stringify({ group_id: groupFiling.groupId }),
        }),
      )
      .then((filed) => filed && onOpen(groupFiling.projectId));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runs once per handed-over filing; actions and callbacks are recreated every render
  }, [groupFiling]);
  const [groupMenu, setGroupMenu] = useState(false);
  const [nameDialog, setNameDialog] = useState<{
    group?: ProjectGroup;
    projectId?: string;
    created?: ProjectGroup;
  } | null>(null);
  const saveGroup = async (name: string) => {
    if (!nameDialog) return;
    if (nameDialog.group) {
      const id = nameDialog.group.id;
      await actions.run(id, () =>
        api(`/api/project-groups/${encodeURIComponent(id)}`, {
          method: "PATCH",
          body: JSON.stringify({ name }),
        }),
      );
      return;
    }
    let saved = nameDialog.created;
    if (!saved) {
      saved = await api<ProjectGroup>("/api/project-groups", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      setNameDialog({ ...nameDialog, created: saved });
    }
    if (saved.name !== name) {
      saved = await api<ProjectGroup>(`/api/project-groups/${encodeURIComponent(saved.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      });
      setNameDialog({ ...nameDialog, created: saved });
    }
    if (nameDialog.projectId) {
      await onGroupsChanged();
      await api(`/api/projects/${encodeURIComponent(nameDialog.projectId)}/group`, {
        method: "PUT",
        body: JSON.stringify({ group_id: saved.id }),
      });
    }
    await onGroupsChanged();
  };
  const machinePower = useMachinePower(identity?.space_kind);
  const [providerLogins, setProviderLogins] = useState<ProviderLoginState[]>([]);
  const [loginError, setLoginError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    loadProviderLogins().then(
      (states) => {
        if (active) {
          setProviderLogins(states.filter((state) => state.agent_used));
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
  const [deletePreview, setDeletePreview] = useState<ProjectDeletionPreview | "failed" | null>(
    null,
  );
  const [deletePreviewAttempt, setDeletePreviewAttempt] = useState(0);
  const [deletedReport, setDeletedReport] = useState<{ name: string; unstopped: string[] } | null>(
    null,
  );
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
      setGroupMenu(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpenMenuProject(null);
      setGroupMenu(false);
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

  useEffect(() => {
    setDeletePreview(null);
    if (!deleteProjectId) return;
    let active = true;
    loadProjectDeletionPreview(deleteProjectId).then(
      (preview) => {
        if (active) setDeletePreview(preview);
      },
      () => {
        if (active) setDeletePreview("failed");
      },
    );
    return () => {
      active = false;
    };
  }, [deleteProjectId, deletePreviewAttempt]);

  const closeDeleteConfirmation = () => {
    if (deleteBusy) return;
    setDeleteProjectId(null);
    setDeleteError(null);
  };

  const confirmDelete = async () => {
    if (!deleteProject || deleteBusy || deletePreview === null || deletePreview === "failed")
      return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      const result = await onDelete(deleteProject.id, deletePreview.stop_token);
      if (result.unstopped.length > 0) {
        setDeletedReport({ name: deleteProject.name, unstopped: result.unstopped });
      }
      setCovers((current) => {
        const next = { ...current };
        delete next[deleteProject.id];
        return next;
      });
      setDeleteProjectId(null);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : String(error));
      // The work to stop may have changed; show the current list before a retry.
      setDeletePreviewAttempt((attempt) => attempt + 1);
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
        <ProviderLoginNotice
          // Another member's own login is theirs to sign in, not this viewer's notice.
          states={providerLogins.filter(
            (state) => !waitsOnOtherMember(state, identity?.user.user_id ?? null),
          )}
          viewerUserId={identity?.user.user_id ?? null}
          onOpenSignIns={onOpenSignIns}
        />
        {loginError && <p role="alert">{loginError}</p>}
        <div
          className={`space-group-title-row${group && actions.failure?.id === group.id ? " group-failed" : ""}`}
        >
          {group ? (
            <nav className="group-breadcrumb" aria-label="Project group">
              <a
                href="#"
                className={drag.over === null ? "group-drop-target" : ""}
                {...drag.target(null)}
              >
                {spaceTitle}
              </a>
              <span aria-hidden="true">›</span>
              <h1 className="space-group-title">{group.name}</h1>
            </nav>
          ) : (
            <h1 className="space-group-title">{spaceTitle}</h1>
          )}
          {group ? (
            <div className="group-header-actions project-cover-shell">
              {actions.retry(group.id)}
              <button
                className="icon-button"
                aria-label="Group actions"
                aria-haspopup="menu"
                aria-expanded={groupMenu}
                onClick={() => setGroupMenu(!groupMenu)}
              >
                <Ellipsis size={18} />
              </button>
              {groupMenu && (
                <div
                  className="project-cover-menu"
                  role="menu"
                  onKeyDown={(event) => {
                    if (event.key === "Escape") setGroupMenu(false);
                  }}
                >
                  <button
                    className="project-cover-menu-action"
                    role="menuitem"
                    onClick={() => {
                      setGroupMenu(false);
                      setNameDialog({ group });
                    }}
                  >
                    Rename
                  </button>
                  {group.count === 0 && (
                    <button
                      className="project-cover-menu-action project-delete-action"
                      role="menuitem"
                      disabled={actions.busy}
                      onClick={() => {
                        setGroupMenu(false);
                        void actions.run(group.id, async () => {
                          await api(`/api/project-groups/${encodeURIComponent(group.id)}`, {
                            method: "DELETE",
                          });
                          window.location.hash = projectGroupHash(null);
                        });
                      }}
                    >
                      Delete
                    </button>
                  )}
                </div>
              )}
            </div>
          ) : (
            <button className="group-create" onClick={() => setNameDialog({})}>
              <FolderPlus size={16} />
              New group
            </button>
          )}
          {!group && identity?.space_kind === "team" && onExitTeamSpace && (
            <button className="team-space-exit" onClick={onExitTeamSpace}>
              <LogOut size={14} />
              Exit team space
            </button>
          )}
        </div>
        <section className="project-shelf" aria-label="RCP projects">
          {!group &&
            invitations.map((invitation) => (
              <ProjectInvitationCard
                key={invitation.invitation_id}
                invitation={invitation}
                onAnswer={onAnswerInvitation}
              />
            ))}
          {shownProjects.map((project) => {
            const unavailable = project.reachable === false;
            const cover = covers[project.id] || "wood";
            return (
              <div
                className={`project-cover-shell${actions.failure?.id === project.id ? " group-failed" : ""}`}
                key={project.id}
              >
                <button
                  className={`project-cover project-material-${cover}`}
                  {...drag.source(project.id)}
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
                {actions.retry(project.id)}
                {openMenuProject === project.id && (
                  <ProjectActionsMenu
                    project={project}
                    groups={groups}
                    onMoveToGroup={(id) => {
                      setOpenMenuProject(null);
                      void actions.move(project.id, id);
                    }}
                    onNewGroup={() => {
                      setOpenMenuProject(null);
                      setNameDialog({ projectId: project.id });
                    }}
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

          {!group &&
            shelf.groups.map((item) => (
              <div className="project-cover-shell box-set-shell" key={item.id}>
                {item.projects.slice(0, 2).map((project, index) => (
                  <span
                    key={project.id}
                    aria-hidden="true"
                    className={`box-set-peek peek-${index} project-material-${covers[project.id] || "wood"}`}
                  />
                ))}
                <button
                  className={`project-cover box-set${drag.over === item.id ? " group-drop-target" : ""}`}
                  {...drag.target(item.id)}
                  onClick={() => {
                    setGroupMenu(false);
                    window.location.hash = projectGroupHash(item.id);
                  }}
                >
                  <span className="box-set-spines" aria-hidden="true">
                    {item.projects.map((project) => (
                      <span
                        key={project.id}
                        className={`project-material-${covers[project.id] || "wood"}`}
                      />
                    ))}
                  </span>
                  <strong>{item.name}</strong>
                  <span className="project-cover-meta">
                    {item.count} {item.count === 1 ? "project" : "projects"}
                    {item.waiting > 0 && ` · ${item.waiting} waiting`}
                  </span>
                </button>
              </div>
            ))}
          <button className="project-cover new-project-cover" onClick={() => onCreate(group?.id)}>
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
          preview={deletePreview}
          busy={deleteBusy}
          error={deleteError}
          onClose={closeDeleteConfirmation}
          onConfirm={confirmDelete}
          onRetryPreview={() => setDeletePreviewAttempt((attempt) => attempt + 1)}
        />
      )}
      {nameDialog && (
        <GroupNameDialog
          name={nameDialog.group?.name ?? ""}
          onClose={() => setNameDialog(null)}
          onSave={saveGroup}
        />
      )}
      {deletedReport && (
        <ProjectDeletedDialog
          name={deletedReport.name}
          unstopped={deletedReport.unstopped}
          onClose={() => setDeletedReport(null)}
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
