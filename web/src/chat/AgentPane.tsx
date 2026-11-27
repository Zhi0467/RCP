import { ChevronDown, Download, File, FileText, Image, Table } from "lucide-react";
import {
  useCallback,
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
import {
  artifactDownloadOnly,
  openArtifact,
  openSpaceArtifact,
} from "../artifacts/artifactViewerModel";
import { ArtifactDownload, storedArtifactSave } from "../artifacts/ArtifactDownload";
import { DeleteArtifactButton } from "../artifacts/DeleteArtifactButton";
import { CopyReferenceButton } from "../core/CopyReferenceButton";
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
  const refresh = useCallback(() => {
    if (id) store.invalidate(id);
  }, [store, id]);
  return { artifacts: resource.value ?? NO_ARTIFACTS, refresh };
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
  onDeleted,
}: {
  title: string;
  endpoints: AgentWindowEndpoints;
  graphTarget: GraphTargetRef;
  content: AgentPaneContent;
  top?: number;
  /** Draws one compact task card for a section's run or parked plan. */
  renderTask: (section: PaneSection, index: number) => ReactNode;
  onOpened: () => void;
  onDeleted: () => void;
}) {
  const artifacts = content.artifacts;
  const projectId = endpoints.projectId;
  const open = (artifact: RunArtifactEntry) => {
    if (projectId === null)
      openSpaceArtifact({
        viewerUrl: endpoints.artifactUrl(
          artifact.origin_operation_id ?? "",
          artifact.artifact_id,
          "viewer",
        ),
        name: artifact.name,
      });
    else openArtifact({ projectId, artifactId: artifact.artifact_id });
    onOpened();
  };
  return (
    <aside className="agent-pane" style={{ top }} aria-label="Tasks and artifacts">
      <header>
        <strong>{title}</strong>
      </header>
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
            const downloadOnly = artifactDownloadOnly(artifact.view, projectId !== null);
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
                <ArtifactDownload
                  name={artifact.name}
                  fileName={artifact.download_name}
                  className="icon-button artifact-entry-action"
                  href={
                    projectId === null
                      ? endpoints.artifactUrl(
                          artifact.origin_operation_id ?? "",
                          artifact.artifact_id,
                          "download",
                        )
                      : `/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}/download`
                  }
                  save={
                    projectId === null
                      ? null
                      : storedArtifactSave(projectId, artifact.artifact_id, artifact.download_name)
                  }
                >
                  <Download size={15} />
                </ArtifactDownload>
                {projectId !== null && artifact.can_delete && (
                  <DeleteArtifactButton
                    projectId={projectId}
                    artifactId={artifact.artifact_id}
                    label={`Delete ${artifact.name}`}
                    className="icon-button artifact-entry-action"
                    iconSize={15}
                    onDeleted={onDeleted}
                  />
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
