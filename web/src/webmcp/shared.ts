import type { AgentTask, GraphNode, ProjectSnapshot } from "../core/types";

export type WebMcpJsonSchema = {
  type: "object";
  properties?: Record<string, unknown>;
  required?: string[];
  additionalProperties: false;
};

export type WebMcpToolResult = {
  content: Array<{ type: "text"; text: string }>;
};

export type WebMcpToolDefinition = {
  name: string;
  description: string;
  inputSchema: WebMcpJsonSchema;
  annotations?: {
    readOnlyHint?: boolean;
    untrustedContentHint?: boolean;
  };
  execute: (
    input: Record<string, unknown>,
    requestId?: string,
  ) => WebMcpToolResult | Promise<WebMcpToolResult>;
};

/** A tool's fixed definition, independent of page state. `confirm` says whether one
 * exact call waits for the member's confirmation in a voice session; `alwaysConfirm`
 * keeps that card even when the member runs without confirming; `voiceOnly` keeps the
 * tool off WebMCP, whose host agents get no RCP card. All three stay local and are
 * never registered with a host. */
export type WebMcpToolSpec = Omit<WebMcpToolDefinition, "execute"> & {
  confirm: (input: Record<string, unknown>) => boolean;
  alwaysConfirm?: true;
  voiceOnly?: true;
};

export const NEVER_CONFIRM = () => false;
export const ALWAYS_CONFIRM = () => true;

export function withExecute(
  spec: WebMcpToolSpec,
  execute: WebMcpToolDefinition["execute"],
): WebMcpToolDefinition {
  const { confirm: _, alwaysConfirm: _always, voiceOnly: _voice, ...definition } = spec;
  return { ...definition, execute };
}

export type WebMcpModelContext = {
  registerTool: (
    definition: WebMcpToolDefinition,
    options?: { signal?: AbortSignal },
  ) => void | Promise<void>;
};

type DocumentWithModelContext = {
  modelContext?: unknown;
};

export type WebMcpRegistration = {
  controller: AbortController;
  dispose: () => void;
};

export type WebMcpToolRegistry = {
  update: (definitions: WebMcpToolDefinition[]) => void;
  dispose: () => void;
};

export const WEBMCP_RESULT_MAX_CHARS = 1_500;

export function modelContextFromDocument(value: unknown): WebMcpModelContext | null {
  if (!value || typeof value !== "object") return null;
  const candidate = (value as DocumentWithModelContext).modelContext;
  if (!candidate || typeof candidate !== "object") return null;
  const registerTool = (candidate as { registerTool?: unknown }).registerTool;
  if (typeof registerTool !== "function") return null;
  return candidate as WebMcpModelContext;
}

export function currentWebMcpContext(): WebMcpModelContext | null {
  return typeof document === "undefined" ? null : modelContextFromDocument(document);
}

function observeWebMcpRegistration(result: void | Promise<void>, signal: AbortSignal): void {
  if (!result) return;
  void result.catch((error: unknown) => {
    if (
      signal.aborted &&
      typeof error === "object" &&
      error !== null &&
      "name" in error &&
      error.name === "AbortError"
    ) {
      return;
    }
    console.error("WebMCP tool registration failed.", error);
  });
}

export function registerWebMcpTools(
  definitions: WebMcpToolDefinition[],
  context: WebMcpModelContext | null = currentWebMcpContext(),
): WebMcpRegistration | null {
  if (!context || definitions.length === 0) return null;
  const controller = new AbortController();
  try {
    definitions.forEach((definition) => {
      observeWebMcpRegistration(
        context.registerTool(definition, { signal: controller.signal }),
        controller.signal,
      );
    });
  } catch (error) {
    controller.abort();
    throw error;
  }
  return {
    controller,
    dispose: () => controller.abort(),
  };
}

export function createWebMcpToolRegistry(
  definitions: WebMcpToolDefinition[],
  context: WebMcpModelContext | null = currentWebMcpContext(),
): WebMcpToolRegistry | null {
  if (!context || definitions.length === 0) return null;
  const current = new Map<string, WebMcpToolDefinition>();
  const registrations = new Map<
    string,
    {
      registration: WebMcpRegistration;
      activeCalls: number;
      retired: boolean;
      retireTimer: ReturnType<typeof setTimeout> | null;
    }
  >();
  let disposed = false;

  const finishCall = (name: string): void => {
    const entry = registrations.get(name);
    if (!entry) return;
    entry.activeCalls -= 1;
    if (!entry.retired || entry.activeCalls !== 0 || entry.retireTimer !== null) return;
    entry.retireTimer = setTimeout(() => {
      entry.retireTimer = null;
      if (!entry.retired || entry.activeCalls !== 0) return;
      entry.registration.dispose();
      registrations.delete(name);
    }, 0);
  };

  const executeCurrent = (
    name: string,
    input: Record<string, unknown>,
  ): WebMcpToolResult | Promise<WebMcpToolResult> => {
    const latest = current.get(name);
    const entry = registrations.get(name);
    if (!latest || !entry || entry.retired) {
      throw new Error(`WebMCP tool ${name} is not currently available.`);
    }
    entry.activeCalls += 1;
    try {
      const result = latest.execute(input);
      if (result && typeof (result as Promise<WebMcpToolResult>).then === "function") {
        return Promise.resolve(result).finally(() => finishCall(name));
      }
      finishCall(name);
      return result;
    } catch (error) {
      finishCall(name);
      throw error;
    }
  };

  const update = (nextDefinitions: WebMcpToolDefinition[]): void => {
    if (disposed) throw new Error("Cannot update a disposed WebMCP tool registry.");
    const nextNames = new Set(nextDefinitions.map((definition) => definition.name));
    for (const name of current.keys()) {
      if (nextNames.has(name)) continue;
      current.delete(name);
      const entry = registrations.get(name);
      if (!entry) continue;
      entry.retired = true;
      if (entry.activeCalls === 0) {
        entry.registration.dispose();
        registrations.delete(name);
      }
    }
    for (const definition of nextDefinitions) {
      current.set(definition.name, definition);
      const existing = registrations.get(definition.name);
      if (existing) {
        existing.retired = false;
        if (existing.retireTimer !== null) {
          clearTimeout(existing.retireTimer);
          existing.retireTimer = null;
        }
        continue;
      }
      const proxy: WebMcpToolDefinition = {
        ...definition,
        execute: (input) => executeCurrent(definition.name, input),
      };
      const registration = registerWebMcpTools([proxy], context);
      if (registration) {
        registrations.set(definition.name, {
          registration,
          activeCalls: 0,
          retired: false,
          retireTimer: null,
        });
      }
    }
  };

  update(definitions);
  return {
    update,
    dispose: () => {
      if (disposed) return;
      disposed = true;
      registrations.forEach((entry) => {
        if (entry.retireTimer !== null) clearTimeout(entry.retireTimer);
        entry.registration.dispose();
      });
      registrations.clear();
      current.clear();
    },
  };
}

export function webMcpTextResult(
  value: unknown,
  maxChars: number = WEBMCP_RESULT_MAX_CHARS,
): WebMcpToolResult {
  const text = JSON.stringify(value);
  if (text === undefined) throw new Error("WebMCP tool result is not JSON serializable.");
  if (text.length > maxChars) {
    throw new Error(`WebMCP tool result exceeds ${maxChars} characters.`);
  }
  return { content: [{ type: "text", text }] };
}

export function compactText(value: string, maxChars: number): string {
  return value.length <= maxChars ? value : `${value.slice(0, maxChars - 1)}…`;
}

export function compactNode(node: GraphNode): Record<string, unknown> {
  return {
    id: node.id,
    type: node.type,
    title: compactText(node.title, 96),
    standing: node.standing,
    ...(node.status ? { status: node.status } : {}),
  };
}

export function requiredStringInput(input: Record<string, unknown>, name: string): string {
  const value = input[name];
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${name} must be a non-blank string.`);
  }
  return value;
}

export function optionalStringInput(input: Record<string, unknown>, name: string): string | null {
  const value = input[name];
  if (value === undefined) return null;
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${name} must be a non-blank string when supplied.`);
  }
  return value;
}

export function stringListInput(
  input: Record<string, unknown>,
  name: string,
  maximum = 32,
): string[] {
  const value = input[name];
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > maximum) {
    throw new Error(`${name} must be an array of at most ${maximum} strings.`);
  }
  const items = value.map((item) => {
    if (typeof item !== "string" || !item.trim()) {
      throw new Error(`${name} must contain only non-blank strings.`);
    }
    return item;
  });
  if (new Set(items).size !== items.length) throw new Error(`${name} must not contain duplicates.`);
  return items;
}

export function taskChatId(task: AgentTask): string | null {
  const value = task.request.chat_id;
  return typeof value === "string" && value ? value : null;
}

export function compactExperimentControl(
  control: ProjectSnapshot["experiment_control"][string],
): Record<string, unknown> {
  const operational = control.operational;
  const episode = control.episode;
  return {
    ready: control.ready,
    reasons: (control.reasons ?? []).slice(0, 4).map((reason) => compactText(reason, 180)),
    graph_reasons: (control.graph_reasons ?? [])
      .slice(0, 4)
      .map((reason) => compactText(reason, 180)),
    invocations: {
      used: control.invocations_used,
      ceiling: control.invocation_ceiling,
      remaining: control.invocations_remaining,
    },
    episode_id: control.episode_id,
    paused: control.paused,
    active: control.active,
    health: control.health,
    recommendation: control.recommendation,
    run_section: control.run_section,
    live: control.live,
    can_start: control.can_start,
    can_stop: control.can_stop,
    stop_pending: control.stop_pending,
    task_control: control.task_control,
    can_switch_provider: control.can_switch_provider,
    can_open_report: control.can_open_report,
    report_episode_id: control.report_episode_id,
    node_closed: control.node_closed,
    governing_decision_ids: (control.governing_decisions ?? [])
      .slice(0, 8)
      .map((decision) => decision.decision_id),
    decision_drift_count: (control.decision_drift ?? []).length,
    operational: operational
      ? {
          task_active: operational.task_active,
          detached_work_active: operational.detached_work_active,
          watcher_degraded: operational.watcher_degraded,
          watcher_completion_pending: operational.watcher_completion_pending,
          episode_exited: operational.episode_exited,
          episode_live: operational.episode_live,
          stop_requested: operational.stop_requested,
          stop_settled: operational.stop_settled,
          chat_id: operational.chat_id,
          current_task_id: operational.current_operation_id,
          current_queued: operational.current_queued,
          current_active: operational.current_active,
          current_awaiting_human: operational.current_awaiting_human,
          current_phase: operational.current_phase,
          current_status_message: operational.current_status_message
            ? compactText(operational.current_status_message, 180)
            : null,
          current_invocation: operational.current_invocation,
          session: {
            provider: operational.session.provider,
            model: operational.session.model,
            reasoning: operational.session.reasoning,
            run_on: operational.session.run_on,
            execution_host: operational.session.execution_host,
            run_truth_scope: operational.session.run_truth_scope?.slice(0, 8) ?? null,
            native_session_bound: operational.session.native_session_bound,
            diagnostic: operational.session.diagnostic
              ? compactText(operational.session.diagnostic, 180)
              : null,
          },
        }
      : null,
    episode: episode
      ? {
          episode_id: episode.episode_id,
          graph_target: episode.graph_target,
          recovery: episode.recovery,
          budget: episode.budget,
          ending: episode.ending,
          ending_diagnostic: episode.ending_diagnostic
            ? compactText(episode.ending_diagnostic, 180)
            : null,
          wrapup_state: episode.wrapup_state,
          wrapup_error: episode.wrapup_error ? compactText(episode.wrapup_error, 180) : null,
          updated_at: episode.updated_at,
          report: episode.report,
        }
      : null,
  };
}
