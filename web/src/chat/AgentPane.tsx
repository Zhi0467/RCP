import { ChevronDown, Download, File, FileText, Image, Table, X } from "lucide-react";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import {
  PANE_SECTIONS,
  paneSectionFolded,
  rememberPaneSectionFolded,
  type AgentPaneContent,
  type PaneSection,
} from "../agents/taskCardModel";
import type { AgentWindowEndpoints } from "../agentWindow/agentWindowEndpoints";
import { chatArtifactsProfile } from "../artifacts/artifactResources";
import { openArtifact, openSpaceArtifact } from "../artifacts/artifactViewerModel";
import { StoredArtifactDownload } from "../artifacts/StoredArtifactDownload";
import { isDesktopRuntime, openDesktopArtifactPdf } from "../core/desktopRuntime";
import { CopyReferenceButton } from "../core/CopyReferenceButton";
import { errorMessage } from "../core/errors";
import { setReferenceDrag } from "../core/projectReferences";
import type { GraphTargetRef, RunArtifactEntry } from "../core/types";
import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime, useResource } from "../core/useResource";
import { relativeTime } from "../projects/commentMentions";

const NO_ARTIFACTS: RunArtifactEntry[] = [];
const BESIDE_MIN_WIDTH = 640;

export function useChatArtifacts(
  agentId: string | null,
  projectId: string | null,
  revision: string | null,
) {
  const owner = useId();
  const { store } = useRefreshRuntime();
  const profile = agentId ? chatArtifactsProfile(agentId, projectId) : null;
  const id = profile ? resourceKeyId(profile.key) : null;
  const seen = useRef<{ id: string | null; revision: string | null } | null>(null);
  useEffect(() => {
    const previous = seen.current;
    seen.current = { id, revision };
    if (!id || previous?.id !== id || previous.revision === null) return;
    if (previous.revision !== revision) store.invalidate(id);
  }, [store, id, revision]);
  const resource = useResource(owner, profile);
  return resource.value ?? NO_ARTIFACTS;
}

/** Where the open pane sits: below the dock's header, beside the chat when the dock
 *  is wide enough, else over it. */
export function useAgentPaneLayout(dock: RefObject<HTMLElement | null>, open: boolean) {
  const [layout, setLayout] = useState<{ top?: number; beside: boolean }>({ beside: false });
  useLayoutEffect(() => {
    const element = dock.current;
    const header = element?.querySelector<HTMLElement>(":scope > header");
    if (!open || !element || !header) return;
    const measure = () =>
      setLayout({ top: header.offsetHeight, beside: element.clientWidth >= BESIDE_MIN_WIDTH });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    observer.observe(header);
    return () => observer.disconnect();
  }, [dock, open]);
  return layout;
}

function TypeIcon({ mediaType }: { mediaType: string }) {
  if (mediaType === "text/csv") return <Table size={16} />;
  if (mediaType.startsWith("image/")) return <Image size={16} />;
  if (mediaType.startsWith("text/")) return <FileText size={16} />;
  return <File size={16} />;
}

const SECTION_TITLES: Record<PaneSection, string> = {
  running: "Running",
  parked: "Parked",
  ended: "Ended",
  artifacts: "Artifacts",
};

function PaneSectionFold({
  section,
  count,
  children,
}: {
  section: PaneSection;
  count: number;
  children: ReactNode;
}) {
  const [folded, setFolded] = useState(() => paneSectionFolded(section));
  const title = SECTION_TITLES[section];
  return (
    <section className="agent-pane-section" data-folded={folded ? "" : undefined}>
      <button
        type="button"
        className="agent-pane-fold"
        aria-expanded={!folded}
        aria-label={title}
        onClick={() => {
          rememberPaneSectionFolded(section, !folded);
          setFolded(!folded);
        }}
      >
        <ChevronDown size={14} aria-hidden="true" />
        <span>{title}</span>
        {folded && <span className="agent-pane-count">{count}</span>}
      </button>
      {!folded && <ul aria-label={title}>{children}</ul>}
    </section>
  );
}

/**
 * The agent's tasks and artifacts beside its transcript: what runs, what is
 * parked, what ended, and what was made. An empty section is left out.
 */
export function AgentPane({
  title,
  endpoints,
  graphTarget,
  content,
  top,
  renderTask,
  onOpened,
  onClose,
}: {
  title: string;
  endpoints: AgentWindowEndpoints;
  graphTarget: GraphTargetRef;
  content: AgentPaneContent;
  top?: number;
  /** Draws one compact task card for a section's run or parked plan. */
  renderTask: (section: PaneSection, index: number) => ReactNode;
  onOpened: () => void;
  onClose: () => void;
}) {
  const artifacts = content.artifacts;
  const projectId = endpoints.projectId;
  const [openError, setOpenError] = useState<string | null>(null);
  const open = (artifact: RunArtifactEntry) => {
    setOpenError(null);
    if (projectId === null)
      openSpaceArtifact({
        viewerUrl: endpoints.artifactUrl(
          artifact.origin_operation_id ?? "",
          artifact.artifact_id,
          "viewer",
        ),
        name: artifact.name,
      });
    else if (artifact.view === "pdf")
      void openDesktopArtifactPdf({ projectId, artifactId: artifact.artifact_id }).catch(
        (failure) => setOpenError(errorMessage(failure)),
      );
    else openArtifact({ projectId, artifactId: artifact.artifact_id });
    onOpened();
  };
  return (
    <aside className="agent-pane" style={{ top }} aria-label="Tasks and artifacts">
      <header>
        <strong>{title}</strong>
        <button className="icon-button" type="button" aria-label="Close" onClick={onClose}>
          <X size={15} />
        </button>
      </header>
      {openError && <div role="alert">{openError}</div>}
      {PANE_SECTIONS.filter((section) => section !== "artifacts").map(
        (section) =>
          content[section].length > 0 && (
            <PaneSectionFold key={section} section={section} count={content[section].length}>
              {content[section].map((_, index) => renderTask(section, index))}
            </PaneSectionFold>
          ),
      )}
      {artifacts.length > 0 && (
        <PaneSectionFold section="artifacts" count={artifacts.length}>
          {artifacts.map((artifact) => {
            const downloadOnly =
              artifact.view === "file" ||
              (artifact.view === "pdf" && (!isDesktopRuntime() || projectId === null));
            const kind = artifact.supplier === "episode_ending" ? "Report" : artifact.view;
            return (
              <li
                key={artifact.artifact_id}
                className="artifact-entry"
                draggable={projectId !== null}
                onDragStart={(event) => {
                  if (projectId)
                    setReferenceDrag(event.dataTransfer, projectId, graphTarget, {
                      kind: "artifact",
                      artifact_id: artifact.artifact_id,
                    });
                }}
              >
                <span className="chat-artifact-icon">
                  <TypeIcon mediaType={artifact.media_type} />
                </span>
                <div>
                  <h3>{artifact.name}</h3>
                  <div className="artifact-entry-meta">
                    <time
                      dateTime={artifact.created_at}
                      title={new Date(artifact.created_at).toLocaleString()}
                    >
                      {[kind, artifact.worker_label, relativeTime(artifact.created_at)]
                        .filter(Boolean)
                        .join(" · ")}
                    </time>
                  </div>
                </div>
                {projectId !== null && (
                  <CopyReferenceButton
                    projectId={projectId}
                    graphTarget={graphTarget}
                    reference={{ kind: "artifact", artifact_id: artifact.artifact_id }}
                    className="icon-button artifact-entry-action"
                  />
                )}
                {projectId === null ? (
                  <a
                    className="icon-button artifact-entry-action"
                    href={endpoints.artifactUrl(
                      artifact.origin_operation_id ?? "",
                      artifact.artifact_id,
                      "download",
                    )}
                    download={artifact.name}
                    aria-label={`Download ${artifact.name}`}
                    title="Download"
                  >
                    <Download size={15} />
                  </a>
                ) : (
                  <StoredArtifactDownload
                    projectId={projectId}
                    artifactId={artifact.artifact_id}
                    name={artifact.name}
                    className="icon-button artifact-entry-action"
                    href={`/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}/download`}
                  >
                    <Download size={15} />
                  </StoredArtifactDownload>
                )}
                {!downloadOnly && (
                  <button
                    type="button"
                    className="artifact-entry-open"
                    aria-label={`Open ${artifact.name}`}
                    onClick={() => open(artifact)}
                  />
                )}
              </li>
            );
          })}
        </PaneSectionFold>
      )}
    </aside>
  );
}
