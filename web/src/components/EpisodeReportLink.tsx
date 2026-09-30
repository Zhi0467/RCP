import type { AnchorHTMLAttributes, MouseEvent, ReactNode } from "react";
import { openEpisodeReport } from "../artifactViewer";

interface Props extends Omit<
  AnchorHTMLAttributes<HTMLAnchorElement>,
  "children" | "href" | "onClick" | "rel" | "target"
> {
  projectId: string;
  episodeId: string;
  href: string;
  children: ReactNode;
  onOpenError: (message: string) => void;
}

export function EpisodeReportLink({
  projectId,
  episodeId,
  href,
  children,
  onOpenError,
  ...anchorProps
}: Props) {
  const openReport = async (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    try {
      await openEpisodeReport({ projectId, episodeId });
    } catch (error) {
      onOpenError(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <a {...anchorProps} href={href} onClick={openReport}>
      {children}
    </a>
  );
}
