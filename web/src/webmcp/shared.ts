import type { GraphNode } from "../core/types";

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
  execute: (input: Record<string, unknown>) => WebMcpToolResult | Promise<WebMcpToolResult>;
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
