import { Archive, ArchiveRestore, LoaderCircle } from "lucide-react";
import { useState } from "react";
import type { AuthorizedHuman, Episode } from "../types";

export type ArchiveEpisodeAction = (
  projectId: string,
  episodeId: string,
  archived: boolean,
) => Promise<void>;

export function EpisodeArchiveButton({
  episode,
  disabled = false,
  onArchive,
}: {
  episode: Pick<Episode, "project_id" | "episode_id" | "archived" | "can_archive">;
  disabled?: boolean;
  onArchive: ArchiveEpisodeAction;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!episode.archived && !episode.can_archive) return null;
  const label = episode.archived ? "Unarchive" : "Archive";
  return (
    <span className="episode-archive-control">
      <button
        className="button secondary compact"
        type="button"
        disabled={disabled || busy}
        onClick={() => {
          setBusy(true);
          setError(null);
          void onArchive(episode.project_id, episode.episode_id, !episode.archived)
            .catch((caught) => setError(caught instanceof Error ? caught.message : String(caught)))
            .finally(() => setBusy(false));
        }}
      >
        {busy ? (
          <LoaderCircle size={12} className="spin" aria-hidden="true" />
        ) : episode.archived ? (
          <ArchiveRestore size={12} aria-hidden="true" />
        ) : (
          <Archive size={12} aria-hidden="true" />
        )}
        {label}
      </button>
      {error && (
        <span className="episode-archive-error" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}

export function EpisodeAuthor({ author }: { author: AuthorizedHuman | null }) {
  if (!author) return null;
  const name = author.display_name;
  return (
    <span className="episode-author" title={`Started by ${name}`}>
      <span className="landing-identity-avatar episode-author-avatar" aria-hidden="true">
        {Array.from(name.trim())[0]?.toLocaleUpperCase() ?? "?"}
      </span>
      <span className="episode-author-name">{name}</span>
    </span>
  );
}
