// Nightly graph consolidation and operational lessons: the project routes and
// the small rules the Inbox and Project settings share.

import { api } from "./api";
import type {
  ConsolidationInboxItem,
  ConsolidationSchedule,
  ConsolidationView,
  Lesson,
} from "./types";

export const DEFAULT_CONSOLIDATION_TIME = "03:00";
export const CONSOLIDATION_RENEWAL_WINDOW_DAYS = 3;
export const LESSON_TEXT_MAX_CHARS = 600;
export const CONSOLIDATION_SETTINGS_ANCHOR = "project-consolidation";

const DAY_MS = 24 * 60 * 60 * 1000;

export function loadConsolidation(apiBase: string): Promise<ConsolidationView> {
  return api<ConsolidationView>(`${apiBase}/consolidation`);
}

export function enableConsolidation(
  apiBase: string,
  localTime: string,
  timezone: string,
): Promise<{ schedule: ConsolidationSchedule }> {
  return api(`${apiBase}/consolidation/schedule`, {
    method: "PUT",
    body: JSON.stringify({ local_time: localTime, timezone }),
  });
}

export function disableConsolidation(apiBase: string): Promise<{ schedule: null }> {
  return api(`${apiBase}/consolidation/schedule`, { method: "DELETE" });
}

export function resolveConsolidationRun(
  apiBase: string,
  runId: string,
  action: "keep" | "dismiss",
): Promise<{ item: ConsolidationInboxItem }> {
  // JSON even when empty: a team space refuses a bodyless mutation.
  return api(`${apiBase}/consolidation/runs/${encodeURIComponent(runId)}/${action}`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function loadLessons(apiBase: string): Promise<{ lessons: Lesson[] }> {
  return api(`${apiBase}/lessons`);
}

export function addLesson(apiBase: string, text: string): Promise<{ lesson: Lesson }> {
  return api(`${apiBase}/lessons`, { method: "POST", body: JSON.stringify({ text }) });
}

export function editLesson(
  apiBase: string,
  lessonId: string,
  text: string,
): Promise<{ lesson: Lesson }> {
  return api(`${apiBase}/lessons/${encodeURIComponent(lessonId)}`, {
    method: "PATCH",
    body: JSON.stringify({ text }),
  });
}

export function deleteLesson(apiBase: string, lessonId: string): Promise<unknown> {
  return api(`${apiBase}/lessons/${encodeURIComponent(lessonId)}`, { method: "DELETE" });
}

/** An expired schedule, or one expiring within the renewal window, asks for renewal. */
export function consolidationNeedsRenewal(
  schedule: ConsolidationSchedule | null,
  now: number,
): boolean {
  if (!schedule) return false;
  if (schedule.expired) return true;
  const expires = Date.parse(schedule.expires_at);
  return !Number.isNaN(expires) && expires - now <= CONSOLIDATION_RENEWAL_WINDOW_DAYS * DAY_MS;
}

export function openConsolidationItems(view: ConsolidationView | null): ConsolidationInboxItem[] {
  return (view?.inbox ?? []).filter((item) => item.state === "open");
}

/** The server counts characters as code points; so does this limit. */
export function lessonTextIsValid(text: string): boolean {
  const trimmed = text.trim();
  return trimmed.length > 0 && [...trimmed].length <= LESSON_TEXT_MAX_CHARS;
}

export function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}
