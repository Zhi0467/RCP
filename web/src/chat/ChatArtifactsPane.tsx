import { X } from "lucide-react";
import { useId } from "react";
import { chatArtifactsProfile } from "../artifacts/artifactResources";
import type { RunArtifactEntry } from "../core/types";
import { useResource } from "../core/useResource";
import { RunArtifacts } from "../experiments/RunArtifacts";

const NO_ARTIFACTS: RunArtifactEntry[] = [];

export function useChatArtifacts(projectId: string | null, chatId: string): RunArtifactEntry[] {
  const owner = useId();
  const resource = useResource(owner, projectId ? chatArtifactsProfile(projectId, chatId) : null);
  return resource.value ?? NO_ARTIFACTS;
}

/** The chat's artifacts beside its transcript; a row opens the viewer. */
export function ChatArtifactsPane({
  projectId,
  artifacts,
  onClose,
}: {
  projectId: string;
  artifacts: RunArtifactEntry[];
  onClose: () => void;
}) {
  return (
    <aside className="chat-artifacts-pane" aria-label="Artifacts">
      <header>
        <strong>Artifacts</strong>
        <button className="icon-button" type="button" aria-label="Close" onClick={onClose}>
          <X size={15} />
        </button>
      </header>
      <RunArtifacts projectId={projectId} artifacts={artifacts} />
    </aside>
  );
}
