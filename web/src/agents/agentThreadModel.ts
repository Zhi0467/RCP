// Pure presentation rules for the member's agent threads: the rail, a turn's
// progress line, approval card titles and the Wakes list.

import type {
  AgentSchedule,
  AgentTask,
  AgentThread,
  ApprovalCard,
  ChatMessage,
  GraphNode,
  WatcherRecord,
} from "../core/types";
import { isExternalWatcherRecord, jobLabel } from "../experiments/runProjection.ts";

/** What a Wakes row waits for and when it fires, named by the node's title when loaded. */
export function watcherWake(
  watcher: WatcherRecord,
  nodes: Record<string, Pick<GraphNode, "title">> | undefined,
): { what: string; when: string } {
  if (isExternalWatcherRecord(watcher))
    return { what: jobLabel(watcher.log_path), when: "When the job ends" };
  const { condition } = watcher;
  const what = nodes?.[condition.node_id]?.title || condition.node_id;
  if (!("status_in" in condition)) return { what, when: "When its Proposal is resolved" };
  const states = condition.status_in;
  const listed =
    states.length > 1 ? `${states.slice(0, -1).join(", ")} or ${states.at(-1)}` : states[0];
  return { what, when: `When it's ${listed}` };
}

/** How long ago, as the Agents list shows it: now, 12m, 3h, then a date. */
export function sinceLabel(timestamp: string | null | undefined, now: number): string {
  const at = timestamp ? Date.parse(timestamp) : Number.NaN;
  if (!Number.isFinite(at)) return "";
  const minutes = Math.max(0, Math.round((now - at) / 60_000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 24 * 60) return `${Math.round(minutes / 60)}h`;
  return new Date(at).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** The rail: open threads only, the Space thread first, as the server ordered the rest. */
export function railThreads(threads: readonly AgentThread[]): AgentThread[] {
  const open = threads.filter((thread) => thread.live_state !== "closed");
  return [
    ...open.filter((thread) => thread.kind === "space"),
    ...open.filter((thread) => thread.kind !== "space"),
  ];
}

/** A turn is live while it is queued or running; the rail shows a spinner then. */
export function threadIsLive(thread: AgentThread): boolean {
  return thread.live_state === "running" || thread.live_state === "queued";
}

/** The rail's right edge: "live" while working, else how long ago it last moved. */
export function threadTimeLabel(thread: AgentThread, now: number): string {
  return threadIsLive(thread) ? "live" : sinceLabel(thread.last_activity_at, now);
}

/** The thread whose deep link was asked for, else the Space thread. */
export function chosenThread(
  threads: readonly AgentThread[],
  threadId: string | null,
): AgentThread | null {
  return (
    threads.find((thread) => thread.thread_id === threadId) ??
    threads.find((thread) => thread.kind === "space") ??
    threads[0] ??
    null
  );
}

export const AGENT_HASH = "#/agent";

export function agentThreadHash(threadId: string | null): string {
  return threadId ? `${AGENT_HASH}/${encodeURIComponent(threadId)}` : AGENT_HASH;
}

/** `#/agent` opens the Agent view; `#/agent/<thread>` opens one thread. */
export function parseAgentHash(hash: string): { threadId: string | null } | null {
  if (hash === AGENT_HASH) return { threadId: null };
  const match = /^#\/agent\/([^/?#]+)$/.exec(hash);
  if (!match) return null;
  try {
    return { threadId: decodeURIComponent(match[1]) || null };
  } catch {
    return null;
  }
}

export type TurnCommand = { id: string; text: string; seconds: number | null };

/** One turn's RCP commands, paired start to exit, for its "Ran 4 commands" line. */
export function turnCommands(task: Pick<AgentTask, "events">): TurnCommand[] {
  const commands = new Map<string, { text: string; start: number; end: number | null }>();
  for (const event of task.events ?? []) {
    if (event.event_kind !== "command") continue;
    const id = event.command_id ?? String(event.event_id);
    const at = Date.parse(event.created_at);
    const known = commands.get(id);
    if (!known) {
      commands.set(id, {
        text: event.message,
        start: at,
        end: event.command_phase === "exit" ? at : null,
      });
    } else if (event.command_phase === "exit") known.end = at;
  }
  return [...commands.entries()].map(([id, { text, start, end }]) => ({
    id,
    text,
    seconds:
      end !== null && Number.isFinite(start) && Number.isFinite(end)
        ? Math.max(0, Math.round((end - start) / 1000))
        : null,
  }));
}

/** The folded line: "Ran 4 commands · 12s", with the total only when every command ended. */
export function turnCommandsSummary(commands: readonly TurnCommand[]): string {
  const noun = commands.length === 1 ? "command" : "commands";
  const total = commands.every((command) => command.seconds !== null)
    ? commands.reduce((sum, command) => sum + (command.seconds ?? 0), 0)
    : null;
  return `Ran ${commands.length} ${noun}${total !== null ? ` · ${total}s` : ""}`;
}

function pinnedText(card: ApprovalCard, key: string): string | null {
  const value = card.pinned[key];
  return typeof value === "string" && value.trim() ? value : null;
}

/** The card's question, built from the exact action it will run. */
export function approvalCardTitle(card: ApprovalCard): string {
  const action = card.action;
  switch (action.kind) {
    case "proposal": {
      const verb = action.decision === "approved" ? "Approve" : "Reject";
      const proposal = card.pinned.proposal as { title?: unknown } | undefined;
      const title =
        typeof proposal?.title === "string" && proposal.title.trim() ? proposal.title : null;
      return title ? `${verb} Proposal: ${title}?` : `${verb} this Proposal?`;
    }
    case "truth_scope":
      return action.writable
        ? `Let agents write to ${action.alias}?`
        : `Make ${action.alias} read-only?`;
    case "auto_research":
      return `Start Auto-research for up to ${action.invocation_ceiling} turns?`;
    case "merge_branch":
      return `Merge branch ${pinnedText(card, "branch_name") ?? action.branch_id} into main?`;
    case "merge_episode":
      return `Merge this run into ${action.target_branch ?? "main"}?`;
  }
}

/** A pinned fact as one detail row; nested values read as compact JSON. */
export function approvalCardDetails(card: ApprovalCard): Array<[string, string]> {
  return Object.entries(card.pinned).map(([key, value]) => [
    key.replaceAll("_", " "),
    typeof value === "string" ? value : JSON.stringify(value),
  ]);
}

export function approvalCardExpired(card: ApprovalCard, now: number): boolean {
  return card.state === "expired" || Date.parse(card.expires_at) <= now;
}

const TIME = /^\d{1,2}$/;

function clock(hour: string, minute: string): string {
  return `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
}

function localDay(at: Date): string {
  return [
    at.getFullYear(),
    String(at.getMonth() + 1).padStart(2, "0"),
    String(at.getDate()).padStart(2, "0"),
  ].join("-");
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** A local time on the 24-hour `HH:MM` clock. */
export function localClock(at: Date): string {
  return clock(String(at.getHours()), String(at.getMinutes()));
}

/** A thread's time divider: today, yesterday, else the weekday and date, with a different year. */
export function timeDividerLabel(at: Date, now: Date): string {
  const time = localClock(at);
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (localDay(at) === localDay(now)) return `Today · ${time}`;
  if (localDay(at) === localDay(yesterday)) return `Yesterday · ${time}`;
  const date = `${WEEKDAYS[at.getDay()]} ${at.getDate()} ${MONTHS[at.getMonth()]}`;
  const year = at.getFullYear() === now.getFullYear() ? "" : ` ${at.getFullYear()}`;
  return `${date}${year} · ${time}`;
}

/** A local `YYYY-MM-DDTHH:MM` as its time today, weekday this week, else its date. */
function wallLabel(local: string, now: Date): string {
  const [day, time = ""] = local.split("T");
  if (day === localDay(now)) return time;
  const date = new Date(`${day}T00:00`);
  const days = (date.getTime() - new Date(`${localDay(now)}T00:00`).getTime()) / 86_400_000;
  return days > 0 && days < 7 ? `${WEEKDAYS[date.getDay()]} ${time}` : `${day} ${time}`;
}

function everyLabel(minutes: number): string {
  const [count, unit] =
    minutes % 1440 === 0
      ? [minutes / 1440, "day"]
      : minutes % 60 === 0
        ? [minutes / 60, "hour"]
        : [minutes, "minute"];
  return count === 1 ? `Every ${unit}` : `Every ${count} ${unit}s`;
}

/** When a schedule next runs, as the Upcoming list shows it. */
export function scheduleWhen(schedule: AgentSchedule, now = new Date()): string {
  if (schedule.at_local) {
    const [day, time = ""] = schedule.at_local.split("T");
    return day === localDay(now) ? `Today ${time}` : `${day} ${time}`;
  }
  if (schedule.every_minutes) return everyLabel(schedule.every_minutes);
  const fields = (schedule.cron ?? "").trim().split(/\s+/);
  if (fields.length !== 5) return schedule.cron ?? "";
  const [minute, hour, dayOfMonth, month, dayOfWeek] = fields;
  if (!TIME.test(minute) || !TIME.test(hour) || dayOfMonth !== "*" || month !== "*")
    return schedule.cron ?? "";
  if (dayOfWeek === "*") return `Daily ${clock(hour, minute)}`;
  if (dayOfWeek === "1-5") return `Weekdays ${clock(hour, minute)}`;
  return schedule.cron ?? "";
}

/** A repeat's bounds: fires left, its last time, and a start still to come. */
export function scheduleLimits(schedule: AgentSchedule, now = new Date()): string[] {
  const limits: string[] = [];
  if (schedule.fires_left != null) limits.push(`${schedule.fires_left} left`);
  if (schedule.until_local) limits.push(`until ${wallLabel(schedule.until_local, now)}`);
  const wall = zoneWall(schedule.timezone, now);
  if (schedule.start_local && wall !== null && schedule.start_local > wall)
    limits.push(`from ${wallLabel(schedule.start_local, now)}`);
  return limits;
}

/** Active and paused schedules, those the member can switch back on, and a done
 *  one-shot whose wake still waits; the rest are history. */
export function upcomingSchedules(schedules: readonly AgentSchedule[]): AgentSchedule[] {
  return schedules.filter(
    (schedule) =>
      schedule.state === "active" ||
      schedule.state === "paused" ||
      schedule.state === "disabled" ||
      (schedule.state === "done" && Boolean(schedule.pending_since)),
  );
}

/** How many will run without the member: active ones and a done one-shot's waiting
 *  wake. Paused and disabled ones wait for a Resume or an Enable. */
export function firingScheduleCount(schedules: readonly AgentSchedule[]): number {
  return schedules.filter(
    (schedule) =>
      schedule.state === "active" || (schedule.state === "done" && Boolean(schedule.pending_since)),
  ).length;
}

/** The wall clock `YYYY-MM-DDTHH:MM` in `timezone`, or null for an unknown zone. */
function zoneWall(timezone: string, now: Date): string | null {
  let parts: Intl.DateTimeFormatPart[];
  try {
    parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(now);
  } catch {
    return null;
  }
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}T${part("hour")}:${part("minute")}`;
}

/** Whether a one-shot's local time has passed in its own zone, so it cannot resume. */
export function oneShotPassed(schedule: AgentSchedule, now = new Date()): boolean {
  if (!schedule.at_local) return false;
  const wall = zoneWall(schedule.timezone, now);
  return wall !== null && schedule.at_local <= wall;
}

/** An instant as the reader's short weekday and time, such as `Mon 09:00`. */
export function scheduleInstant(iso: string): string {
  const at = new Date(iso);
  return Number.isNaN(at.getTime())
    ? ""
    : at.toLocaleString([], {
        weekday: "short",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      });
}

/** The operation the newest member message started, which is the thread's latest turn. */
export function latestTurnId(messages: readonly ChatMessage[]): string | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const id = messages[index].operation_id;
    if (id) return id;
  }
  return null;
}

/** A handoff's settlement, as the Space thread's wake reports it. */
export interface HandoffReport {
  threadId: string | null;
  projectId: string | null;
  status: string;
}

/** The handoff report a wake line carries, or null for any other line.
 *
 * The wake's source names it; its text is a summary line, then the report as JSON
 * (`render_handoff_report` in src/rcp/runs/agent_thread_commands.py).
 */
export function handoffReport(line: {
  text: string;
  source?: string | null;
}): HandoffReport | null {
  if (line.source !== "handoff_report") return null;
  try {
    const report: unknown = JSON.parse(line.text.slice(line.text.indexOf("\n") + 1));
    if (!report || typeof report !== "object") return null;
    const value = report as Record<string, unknown>;
    const text = (key: string) => (typeof value[key] === "string" ? (value[key] as string) : null);
    const status = text("status");
    return status
      ? {
          threadId: text("thread_id"),
          projectId: text("project_id"),
          status,
        }
      : null;
  } catch {
    return null;
  }
}

/** The browser's IANA zone, sent with each message so schedules default to it. */
export function browserTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}
