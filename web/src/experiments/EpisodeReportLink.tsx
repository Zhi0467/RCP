import { useEffect, useState } from "react";
import { api } from "../core/api";
import { MAIN_GRAPH } from "../core/graphTarget";
import { setReferenceDrag } from "../core/projectReferences";
import type { GraphTargetRef, RunArtifactEntry } from "../core/types";
import type { AnchorHTMLAttributes, MouseEvent, ReactNode } from "react";
import { openEpisodeReport } from "../artifacts/artifactViewer";

interface Props extends Omit<
  AnchorHTMLAttributes<HTMLAnchorElement>,
  "children" | "href" | "onClick" | "rel" | "target"
> {
  projectId: string;
  graphTarget?: GraphTargetRef;
  episodeId: string;
  href: string;
  children: ReactNode;
  onOpenError: (message: string) => void;
}

export function EpisodeReportLink({
  projectId,
  graphTarget = MAIN_GRAPH,
  episodeId,
  href,
  children,
  onOpenError,
  ...anchorProps
}: Props) {
  const [report, setReport] = useState<{
    projectId: string;
    episodeId: string;
    artifactId: string;
  } | null>(null);
  const artifactId =
    report?.projectId === projectId && report.episodeId === episodeId ? report.artifactId : null;
  useEffect(() => {
    const controller = new AbortController();
    void api<RunArtifactEntry[]>(
      `/api/projects/${encodeURIComponent(projectId)}/episodes/${encodeURIComponent(episodeId)}/artifacts`,
      { signal: controller.signal },
    )
      .then((artifacts) => {
        const entry = artifacts.find((artifact) => artifact.supplier === "episode_ending");
        if (!controller.signal.aborted && entry)
          setReport({ projectId, episodeId, artifactId: entry.artifact_id });
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          console.warn("Could not load episode report reference", error);
      });
    return () => controller.abort();
  }, [projectId, episodeId]);

  const openReport = async (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    try {
      await openEpisodeReport({ projectId, episodeId });
    } catch (error) {
      onOpenError(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <a
      {...anchorProps}
      href={href}
      onClick={openReport}
      draggable={Boolean(artifactId)}
      onDragStart={(event) => {
        if (!artifactId) {
          event.preventDefault();
          return;
        }
        setReferenceDrag(event.dataTransfer, projectId, graphTarget, {
          kind: "artifact",
          artifact_id: artifactId,
        });
      }}
    >
      {children}
    </a>
  );
}
