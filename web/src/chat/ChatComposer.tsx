import { useCallback, useLayoutEffect, useRef, type ComponentProps, type ReactNode } from "react";
import { SkillPicker, type SkillPickerProps } from "../core/SkillPicker";
import type { ConversationMode } from "../core/types";
import { StagedAnswerComments, type AnswerComments } from "./answerComments";
import {
  ComposerAddPicker,
  ComposerAttachmentChips,
  type ComposerAttachments,
} from "./composerAttachments";

/**
 * The one composer shell chat and the Paper coach share: skills, the provider
 * switch hint, staged comments and files, then one row of Add, the message field
 * and the send actions. The caller's own tools and notice take a second row, which
 * exists only when the caller has something for it.
 */
export function ChatComposer({
  hint,
  mode,
  composer,
  comments,
  skills,
  addDisabled = false,
  className,
  field,
  tools,
  actions,
  notice,
}: {
  /** A caller-owned hint above staged comments, such as a provider switch. */
  hint?: ReactNode;
  mode: ConversationMode;
  composer?: ComposerAttachments;
  comments: AnswerComments;
  skills?: SkillPickerProps;
  addDisabled?: boolean;
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
      className={`chat-composer${className ? ` ${className}` : ""}${composer?.dragging ? " is-dragging-files" : ""}`}
      data-mode={mode}
      {...composer?.dropTarget()}
    >
      {skills && <SkillPicker {...skills} />}
      {hint}
      <StagedAnswerComments comments={comments} />
      {composer && <ComposerAttachmentChips composer={composer} />}
      <div className="chat-composer-row">
        {composer && <ComposerAddPicker composer={composer} disabled={addDisabled} />}
        {field}
        <div className="chat-send-actions">{actions}</div>
      </div>
      {(tools || notice) && (
        <div className="chat-composer-tools">
          {tools}
          {notice}
        </div>
      )}
    </div>
  );
}

/**
 * The message field, one line tall until its text needs more. WebKit has no
 * `field-sizing: content` on every macOS the app supports, so the height is
 * measured here and eased by the stylesheet's height transition.
 */
export function ComposerField({
  ref,
  value,
  ...props
}: Omit<ComponentProps<"textarea">, "value"> & { value: string }) {
  const own = useRef<HTMLTextAreaElement | null>(null);
  const attach = useCallback(
    (node: HTMLTextAreaElement | null) => {
      own.current = node;
      if (typeof ref === "function") ref(node);
      else if (ref) ref.current = node;
    },
    [ref],
  );
  useLayoutEffect(() => {
    const field = own.current;
    if (!field) return;
    const from = field.style.height;
    field.style.transition = "none";
    field.style.height = "auto";
    const to = `${field.scrollHeight}px`;
    field.style.height = from;
    // Commit the old height before easing to the new one, so the change never jumps.
    void field.offsetHeight;
    field.style.transition = "";
    field.style.height = to;
  }, [value]);
  return <textarea {...props} ref={attach} rows={1} value={value} />;
}
