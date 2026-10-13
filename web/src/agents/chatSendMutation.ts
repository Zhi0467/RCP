import { ApiError } from "../core/api";
import { chatResourceKey } from "../chat/chatResources";
import type { ResourceStore } from "../core/resourceStore";
import type {
  AgentTask,
  AgentTaskKind,
  AgentTaskRequest,
  ChatTranscript,
  GraphTargetRef,
} from "../core/types";
import { tasksResourceKey } from "./taskResources";

export function beginChatSend(
  store: ResourceStore,
  projectId: string,
  target: GraphTargetRef,
  kind: AgentTaskKind,
  request: AgentTaskRequest,
  requestId: string,
) {
  const id = tasksResourceKey(projectId);
  const ref = target.kind === "main" ? "main" : `branch:${target.branch_id}`;
  const chatId = request.chat_id ? chatResourceKey(projectId, ref, request.chat_id) : null;
  const operationId = `pending-${requestId}`;
  const timestamp = new Date().toISOString();
  const task: AgentTask = {
    operation_id: operationId,
    project_id: projectId,
    graph_target: target,
    kind,
    request,
    // Local-only card: rendering uses the explicit flags below, not this opaque marker.
    status: "pending" as unknown as AgentTask["status"],
    status_label: "Sending",
    status_message: "Sending",
    created_at: timestamp,
    updated_at: timestamp,
    attempt: 0,
    runtime_id: "",
    runtime_label: "",
    provider_label: "",
    history_only: false,
    estimate_seconds: 0,
    estimate_samples: 0,
    elapsed_seconds: 0,
    phase: "Sending",
    progress: 0,
    can_pause: false,
    can_resume: false,
    can_retry: false,
    steer_visible: false,
    can_steer: false,
    steer_unavailable_reason: null,
    steer_turn_id: null,
    steer_action_label: "",
    active: true,
    queued: true,
    pausing: false,
    awaiting_human: false,
    paused: false,
    failed: false,
    settled: false,
    finished: false,
  };
  const effects = new Map<string, (value: unknown) => unknown>([
    [
      id,
      (value) => [
        task,
        ...((value as AgentTask[] | undefined) ?? []).filter(
          (row) => row.operation_id !== operationId,
        ),
      ],
    ],
  ]);
  if (chatId)
    effects.set(chatId, (value) => {
      const transcript = value as ChatTranscript | undefined;
      if (!transcript) return value;
      return {
        ...transcript,
        messages: [
          ...transcript.messages.filter((message) => message.message_id !== operationId),
          {
            message_id: operationId,
            operation_id: operationId,
            role: "user",
            text: request.message ?? "",
            timestamp,
            mode: request.mode ?? null,
            attachments: request.attachments ?? [],
            active_compute_ids: request.active_compute_ids ?? [],
            native_session_id: null,
            provider: null,
            model: null,
            reasoning: null,
            execution_machine: null,
            applied_revision: null,
            graph_update: null,
            trigger: request.trigger ?? "human",
          },
        ],
      };
    });
  const mutation = store.beginMutation(requestId, effects);
  return {
    settle(next: AgentTask) {
      const current = store.committedValue<AgentTask[]>(id) ?? [];
      mutation.settle(
        new Map([
          [
            id,
            [
              next,
              ...current.filter(
                (row) => row.operation_id !== operationId && row.operation_id !== next.operation_id,
              ),
            ],
          ],
        ]),
      );
    },
    fail(error: unknown) {
      // A received rejection is definite. A missing response retains this exact
      // operation overlay and admission key until Resend settles it.
      if (error instanceof ApiError && error.status < 500) mutation.fail();
    },
  };
}
