import { File, FileText, Image, Table, X } from "lucide-react";
import { useId, useLayoutEffect, useRef, useState } from "react";
import { chatArtifactsProfile } from "../artifacts/artifactResources";
import { openArtifact } from "../artifacts/artifactViewerModel";
import type { RunArtifactEntry } from "../core/types";
import { useResource } from "../core/useResource";
import { useNarrowViewport } from "../ui/useNarrowViewport";

const NO_ARTIFACTS: RunArtifactEntry[] = [];

export function useChatArtifacts(projectId: string | null, chatId: string): RunArtifactEntry[] {
  const owner = useId();
  const resource = useResource(owner, projectId ? chatArtifactsProfile(projectId, chatId) : null);
  return resource.value ?? NO_ARTIFACTS;
}

function ArtifactTypeIcon({ mediaType }: { mediaType: string }) {
  if (mediaType === "text/csv") return <Table size={15} />;
  if (mediaType.startsWith("image/")) return <Image size={15} />;
  if (mediaType.startsWith("text/")) return <FileText size={15} />;
  return <File size={15} />;
}

export function ChatArtifactsPane({
  projectId,
  artifacts,
  open,
  onClose,
}: {
  projectId: string;
  artifacts: RunArtifactEntry[];
  open: boolean;
  onClose: () => void;
}) {
  const phone = useNarrowViewport();
  const paneRef = useRef<HTMLElement>(null);
  const [top, setTop] = useState<number>();
  useLayoutEffect(() => {
    const header = paneRef.current?.parentElement?.querySelector<HTMLElement>(":scope > header");
    if (!header) return;
    const observer = new ResizeObserver(() => setTop(header.offsetHeight));
    observer.observe(header);
    return () => observer.disconnect();
  }, []);
  return (
    <aside
      ref={paneRef}
      style={{ top }}
      className={`chat-artifacts-pane${open ? " is-open" : ""}`}
      aria-label="Artifacts"
      inert={!open}
    >
      <header>
        <strong>Artifacts</strong>
        <button className="icon-button" type="button" aria-label="Close" onClick={onClose}>
          <X size={15} />
        </button>
      </header>
      <ul>
        {artifacts.map((artifact) => (
          <li key={artifact.artifact_id}>
            <button
              type="button"
              title={new Date(artifact.created_at).toLocaleString()}
              onClick={() => {
                openArtifact({ projectId, artifactId: artifact.artifact_id });
                if (phone) onClose();
              }}
            >
              <span className="chat-artifacts-pane-icon">
                <ArtifactTypeIcon mediaType={artifact.media_type} />
              </span>
              <span className="chat-artifacts-pane-name">{artifact.name}</span>
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
}
