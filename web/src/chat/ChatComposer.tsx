import { useCallback, useLayoutEffect, useRef, type ComponentProps, type ReactNode } from "react";
import { SkillPicker, type SkillPickerProps } from "../core/SkillPicker";
import { StagedAnswerComments, type AnswerComments } from "./answerComments";
import { DockedComments, type CommentDock } from "../comments/react/CommentDock";
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
  composer,
  comments,
  docked,
  skills,
  addDisabled = false,
  addOptions,
  addOptionCount,
  className,
  field,
  tools,
  actions,
  notice,
}: {
  /** A caller-owned hint above staged comments, such as a provider switch. */
  hint?: ReactNode;
  composer?: ComposerAttachments;
  comments?: AnswerComments;
  /** Saved comment threads the next message carries. */
  docked?: CommentDock;
  skills?: SkillPickerProps;
  addDisabled?: boolean;
  /** The next turn's settings, listed in the Add menu. */
  addOptions?: ReactNode;
  addOptionCount?: number;
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
      {...composer?.dropTarget()}
    >
      {skills && <SkillPicker {...skills} />}
      {hint}
      {comments && <StagedAnswerComments comments={comments} />}
      {docked && <DockedComments dock={docked} busy={false} />}
      {composer && <ComposerAttachmentChips composer={composer} />}
      <div className="chat-composer-row">
        {composer && (
          <ComposerAddPicker
            composer={composer}
            disabled={addDisabled}
            options={addOptions}
            optionCount={addOptionCount}
          />
        )}
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
