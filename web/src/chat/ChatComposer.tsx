import type { ReactNode } from "react";
import { SkillPicker, type SkillPickerProps } from "../core/SkillPicker";
import type { AgentRunConfig, AgentTask, ConversationMode, ProjectSnapshot } from "../core/types";
import { StagedAnswerComments, type AnswerComments } from "./answerComments";
import {
  ComposerAddPicker,
  ComposerAttachmentChips,
  type ComposerAttachments,
} from "./composerAttachments";
import { ProviderSwitchHint } from "./ProviderSwitchHint";

/**
 * The one composer shell chat and the Paper coach share: skills, the provider
 * switch hint, staged comments and files, the message field, then the tools row
 * (Add first, then the caller's own tools) and the send actions.
 */
export function ChatComposer({
  project,
  config,
  tasks,
  sessionId,
  mode,
  composer,
  comments,
  skills,
  addDisabled,
  className,
  field,
  tools,
  actions,
  notice,
}: {
  project: ProjectSnapshot;
  config: AgentRunConfig;
  tasks: AgentTask[];
  sessionId: string | null | undefined;
  mode: ConversationMode;
  composer: ComposerAttachments;
  comments: AnswerComments;
  skills: SkillPickerProps;
  addDisabled: boolean;
  /** The caller's own state, such as a failure edge. */
  className?: string;
  /** The message field, which each caller keys and submits its own way. */
  field: ReactNode;
  tools: ReactNode;
  actions: ReactNode;
  /** A status beside the send actions, such as dictation's. */
  notice?: ReactNode;
}) {
  return (
    <div
      className={`chat-composer${className ? ` ${className}` : ""}${composer.dragging ? " is-dragging-files" : ""}`}
      data-mode={mode}
      {...composer.dropTarget()}
    >
      <SkillPicker {...skills} />
      <ProviderSwitchHint project={project} config={config} tasks={tasks} sessionId={sessionId} />
      <StagedAnswerComments comments={comments} />
      <ComposerAttachmentChips composer={composer} />
      {field}
      <div className="chat-send">
        <div className="chat-composer-tools">
          <ComposerAddPicker composer={composer} disabled={addDisabled} />
          {tools}
        </div>
        <div className="chat-send-actions">{actions}</div>
        {notice}
      </div>
    </div>
  );
}
