// Pure presentation rules for the member's agent threads: the rail, a turn's
// progress line, approval card titles and the Upcoming list.

import type {
  AgentSchedule,
  AgentTask,
  AgentThread,
  ApprovalCard,
  ChatMessage,
} from "../core/types";

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
      const title = pinnedText(card, "title");
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

/** When a schedule next runs, as the Upcoming list shows it. */
export function scheduleWhen(schedule: AgentSchedule, now = new Date()): string {
  if (schedule.at_local) {
    const [day, time = ""] = schedule.at_local.split("T");
    const today = [
      now.getFullYear(),
      String(now.getMonth() + 1).padStart(2, "0"),
      String(now.getDate()).padStart(2, "0"),
    ].join("-");
    return day === today ? `Today ${time}` : `${day} ${time}`;
  }
  const fields = (schedule.cron ?? "").trim().split(/\s+/);
  if (fields.length !== 5) return schedule.cron ?? "";
  const [minute, hour, dayOfMonth, month, dayOfWeek] = fields;
  if (!TIME.test(minute) || !TIME.test(hour) || dayOfMonth !== "*" || month !== "*")
    return schedule.cron ?? "";
  if (dayOfWeek === "*") return `Daily ${clock(hour, minute)}`;
  if (dayOfWeek === "1-5") return `Weekdays ${clock(hour, minute)}`;
  return schedule.cron ?? "";
}

/** Active schedules and those the member can switch back on; the rest are history. */
export function upcomingSchedules(schedules: readonly AgentSchedule[]): AgentSchedule[] {
  return schedules.filter(
    (schedule) => schedule.state === "active" || schedule.state === "disabled",
  );
}

/** The newest message time, which Mark read sends as `read_through`. */
export function readThrough(messages: readonly ChatMessage[]): string | null {
  return messages.at(-1)?.timestamp ?? null;
}

/** The operation the newest member message started, which is the thread's latest turn. */
export function latestTurnId(messages: readonly ChatMessage[]): string | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const id = messages[index].operation_id;
    if (id) return id;
  }
  return null;
}

/** The browser's IANA zone, sent with each message so schedules default to it. */
export function browserTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}
