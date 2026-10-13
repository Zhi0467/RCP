import { graphSessionKey, MAIN_GRAPH } from "../core/graphTarget";
import type { GraphTargetRef } from "../core/types";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useResource } from "../core/useResource";
import { historyProfile } from "./graphResources";
import type { RevisionSummary, ValidationMessage } from "../core/types";

export interface ProjectHistoryUiState {
  projectHistoryOpen: boolean;
  dismissedHistoryNoticeIds: Set<string>;
}

export interface ProjectHistorySnapshot extends ProjectHistoryUiState {
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
  snapshot: ProjectHistoryUiState,
): ProjectHistoryUiState {
  return {
    projectHistoryOpen: snapshot.projectHistoryOpen,
    dismissedHistoryNoticeIds: new Set(snapshot.dismissedHistoryNoticeIds),
  };
}

export function validationNoticeId(message: ValidationMessage): string {
  return JSON.stringify([message.code, message.patch_revision ?? null, message.message]);
}

export function useProjectHistory({
  projectId,
  graphTarget = MAIN_GRAPH,
  loadedProjectId,
  revision,
  reportError,
}: UseProjectHistoryOptions) {
  const [projectHistoryOpen, setProjectHistoryOpen] = useState(false);
  const [dismissedHistoryNoticeIds, setDismissedHistoryNoticeIds] = useState<Set<string>>(() =>
    readDismissedHistoryNoticeIds(projectId, graphTarget),
  );

  const latest = useResource(
    `${projectId}:history-view`,
    projectId && loadedProjectId === projectId ? historyProfile(projectId, graphTarget) : null,
  );
  const history = useResource(
    `${projectId}:history-panel`,
    projectId && projectHistoryOpen ? historyProfile(projectId, graphTarget) : null,
  );
  const latestRevisionSummary =
    latest.value?.find((summary) => summary.to_revision === revision) ?? null;
  const historyRevisionSummaries = useMemo(() => history.value ?? [], [history.value]);
  const historySummariesRevision = history.value ? revision : null;
  const historySummariesError = history.error ? String(history.error) : null;
  useEffect(() => {
    if (history.error) reportError(String(history.error));
  }, [history.error, reportError]);

  const openProjectHistory = useCallback(() => {
    setProjectHistoryOpen(true);
  }, []);

  const closeProjectHistory = useCallback(() => {
    setProjectHistoryOpen(false);
  }, []);

  const resetProjectHistory = useCallback(
    (nextProjectId: string | null, nextTarget: GraphTargetRef = MAIN_GRAPH) => {
      setProjectHistoryOpen(false);
      setDismissedHistoryNoticeIds(readDismissedHistoryNoticeIds(nextProjectId, nextTarget));
    },
    [],
  );

  const restoreProjectHistory = useCallback((snapshot: ProjectHistoryUiState) => {
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
