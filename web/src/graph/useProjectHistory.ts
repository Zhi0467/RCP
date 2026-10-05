import { graphSessionKey, graphTargetUrl, MAIN_GRAPH } from "../graphTarget";
import type { GraphTargetRef } from "../types";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { RevisionSummary, ValidationMessage } from "../types";

export interface ProjectHistorySnapshot {
  latestRevisionSummary: RevisionSummary | null;
  historyRevisionSummaries: RevisionSummary[];
  historySummariesRevision: number | null;
  historySummariesError: string | null;
  projectHistoryOpen: boolean;
  dismissedHistoryNoticeIds: Set<string>;
}

interface UseProjectHistoryOptions {
  projectId: string | null;
  apiBase: string;
  graphTarget?: GraphTargetRef;
  loadedProjectId: string | null;
  revision: number;
  isActiveProject: (projectId: string) => boolean;
  reportError: (message: string) => void;
}

export function revisionSummariesUrl(apiBase: string, revision?: number): string {
  const path = `${apiBase}/history/summaries`;
  return revision === undefined
    ? path
    : `${path}?from_revision=${revision}&to_revision=${revision}`;
}

export function cloneProjectHistorySnapshot(
  snapshot: ProjectHistorySnapshot,
): ProjectHistorySnapshot {
  return {
    ...snapshot,
    historyRevisionSummaries: [...snapshot.historyRevisionSummaries],
    dismissedHistoryNoticeIds: new Set(snapshot.dismissedHistoryNoticeIds),
  };
}

export function validationNoticeId(message: ValidationMessage): string {
  return JSON.stringify([message.code, message.patch_revision ?? null, message.message]);
}

export function useProjectHistory({
  projectId,
  apiBase,
  graphTarget = MAIN_GRAPH,
  loadedProjectId,
  revision,
  isActiveProject,
  reportError,
}: UseProjectHistoryOptions) {
  const [latestRevisionSummary, setLatestRevisionSummary] = useState<RevisionSummary | null>(null);
  const [historyRevisionSummaries, setHistoryRevisionSummaries] = useState<RevisionSummary[]>([]);
  const [historySummariesRevision, setHistorySummariesRevision] = useState<number | null>(null);
  const [historySummariesError, setHistorySummariesError] = useState<string | null>(null);
  const [projectHistoryOpen, setProjectHistoryOpen] = useState(false);
  const [dismissedHistoryNoticeIds, setDismissedHistoryNoticeIds] = useState<Set<string>>(() =>
    readDismissedHistoryNoticeIds(projectId, graphTarget),
  );

  useEffect(() => {
    if (!projectId || !apiBase || loadedProjectId !== projectId) return;
    const requestedProjectId = projectId;
    const requestedRevision = revision;
    if (requestedRevision === 0) {
      setLatestRevisionSummary(null);
      return;
    }
    let cancelled = false;
    setLatestRevisionSummary((current) =>
      current?.to_revision === requestedRevision ? current : null,
    );
    void api<RevisionSummary[]>(
      graphTargetUrl(revisionSummariesUrl(apiBase, requestedRevision), graphTarget),
    )
      .then((summaries) => {
        if (cancelled || !isActiveProject(requestedProjectId)) return;
        setLatestRevisionSummary(
          summaries.find((summary) => summary.to_revision === requestedRevision) ?? null,
        );
      })
      .catch((error) => {
        if (cancelled || !isActiveProject(requestedProjectId)) return;
        setLatestRevisionSummary(null);
        reportError(
          `Latest project change could not be loaded: ${error instanceof Error ? error.message : String(error)}`,
        );
      });
    return () => {
      cancelled = true;
    };
  }, [apiBase, graphTarget, revision, loadedProjectId, projectId]);

  useEffect(() => {
    if (!projectHistoryOpen || !projectId || !apiBase || loadedProjectId !== projectId) return;
    const requestedProjectId = projectId;
    const requestedRevision = revision;
    let cancelled = false;
    setHistorySummariesError(null);
    void api<RevisionSummary[]>(graphTargetUrl(revisionSummariesUrl(apiBase), graphTarget))
      .then((summaries) => {
        if (cancelled || !isActiveProject(requestedProjectId)) return;
        setHistoryRevisionSummaries(summaries);
        setHistorySummariesRevision(requestedRevision);
      })
      .catch((error) => {
        if (cancelled || !isActiveProject(requestedProjectId)) return;
        const message = `Project history could not be loaded: ${error instanceof Error ? error.message : String(error)}`;
        setHistoryRevisionSummaries([]);
        setHistorySummariesError(message);
        setHistorySummariesRevision(requestedRevision);
        reportError(message);
      });
    return () => {
      cancelled = true;
    };
  }, [apiBase, graphTarget, revision, loadedProjectId, projectHistoryOpen, projectId]);

  const openProjectHistory = useCallback(() => {
    setHistorySummariesRevision(null);
    setHistorySummariesError(null);
    setProjectHistoryOpen(true);
  }, []);

  const closeProjectHistory = useCallback(() => {
    setProjectHistoryOpen(false);
  }, []);

  const resetProjectHistory = useCallback(
    (nextProjectId: string | null, nextTarget: GraphTargetRef = MAIN_GRAPH) => {
      setLatestRevisionSummary(null);
      setHistoryRevisionSummaries([]);
      setHistorySummariesRevision(null);
      setHistorySummariesError(null);
      setProjectHistoryOpen(false);
      setDismissedHistoryNoticeIds(readDismissedHistoryNoticeIds(nextProjectId, nextTarget));
    },
    [],
  );

  const restoreProjectHistory = useCallback((snapshot: ProjectHistorySnapshot) => {
    setLatestRevisionSummary(snapshot.latestRevisionSummary);
    setHistoryRevisionSummaries([...snapshot.historyRevisionSummaries]);
    setHistorySummariesRevision(snapshot.historySummariesRevision);
    setHistorySummariesError(snapshot.historySummariesError);
    setProjectHistoryOpen(snapshot.projectHistoryOpen);
    setDismissedHistoryNoticeIds(new Set(snapshot.dismissedHistoryNoticeIds));
  }, []);

  const dismissHistoryNotices = useCallback(
    (messages: ValidationMessage[]) => {
      const ids = messages.map(validationNoticeId);
      setDismissedHistoryNoticeIds((current) => {
        const next = new Set(current);
        ids.forEach((id) => next.add(id));
        try {
          localStorage.setItem(
            historyNoticeStorageKey(projectId ? graphSessionKey(projectId, graphTarget) : null),
            JSON.stringify([...next].sort()),
          );
        } catch {}
        return next;
      });
    },
    [projectId, graphTarget],
  );

  const snapshot = useMemo<ProjectHistorySnapshot>(
    () => ({
      latestRevisionSummary,
      historyRevisionSummaries,
      historySummariesRevision,
      historySummariesError,
      projectHistoryOpen,
      dismissedHistoryNoticeIds,
    }),
    [
      dismissedHistoryNoticeIds,
      historyRevisionSummaries,
      historySummariesError,
      historySummariesRevision,
      latestRevisionSummary,
      projectHistoryOpen,
    ],
  );

  return {
    snapshot,
    openProjectHistory,
    closeProjectHistory,
    resetProjectHistory,
    restoreProjectHistory,
    dismissHistoryNotices,
  };
}

function historyNoticeStorageKey(projectId: string | null): string {
  return `rcp:dismissed-history-notices:${projectId ?? "none"}`;
}

function readDismissedHistoryNoticeIds(
  projectId: string | null,
  graphTarget: GraphTargetRef,
): Set<string> {
  try {
    const parsed: unknown = JSON.parse(
      localStorage.getItem(
        historyNoticeStorageKey(projectId ? graphSessionKey(projectId, graphTarget) : null),
      ) ?? "[]",
    );
    return new Set(
      Array.isArray(parsed)
        ? parsed.filter((item): item is string => typeof item === "string" && item.length > 0)
        : [],
    );
  } catch {
    return new Set();
  }
}
