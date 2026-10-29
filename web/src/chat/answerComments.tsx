import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { MessageCircle, MessageCirclePlus, X } from "lucide-react";
import {
  annotatableAnswerSelectionRange,
  chatAnnotationComposerPosition,
  chatAnnotationTextControlSelection,
  chatAnnotationViewportMetrics,
  chatSelectionCommentPosition,
  MAX_CHAT_ANNOTATIONS,
  MAX_CHAT_ANNOTATION_COMMENT_LENGTH,
  MAX_CHAT_ANNOTATION_TEXT_LENGTH,
  parseStagedChatAnnotations,
  stagedChatAnnotationsAreComplete,
  type ChatAnnotationAnchor,
  type ChatAnnotationComposerPosition,
  type ChatAnnotationViewportMetrics,
  type StagedArtifactTarget,
  type StagedChatAnnotation,
} from "./chatInput";

/** Comments on selected answer text, staged for the next turn: chat and the Paper coach
 * share this whole flow. A selection inside a `.chat-annotatable-answer` offers Comment,
 * and the answer's Comment button opens the same composer from the keyboard. */

interface SelectedAnswerComment {
  step: "comment";
  selectedText: string;
  anchor: ChatAnnotationAnchor;
  position: ChatAnnotationComposerPosition | null;
  /** Set when the selection is a part of an artifact shown inside a reply. */
  artifact?: StagedArtifactTarget;
}

interface KeyboardAnswerComment {
  step: "select";
  answerText: string;
  selectedText: string;
  anchor: ChatAnnotationAnchor;
  position: ChatAnnotationComposerPosition | null;
}

export type AnswerCommentComposer = SelectedAnswerComment | KeyboardAnswerComment;

function readStagedAnnotations(key: string | null): StagedChatAnnotation[] {
  if (!key) return [];
  try {
    return parseStagedChatAnnotations(sessionStorage.getItem(key));
  } catch {
    return [];
  }
}

function writeStagedAnnotations(key: string | null, annotations: StagedChatAnnotation[]): void {
  if (!key) return;
  try {
    if (annotations.length) sessionStorage.setItem(key, JSON.stringify(annotations));
    else sessionStorage.removeItem(key);
  } catch {}
}

export function useAnswerComments({
  storageKey,
  enabled,
  busy,
  transcript,
  message,
  onError,
}: {
  /** Where staged comments persist for this tab, or null to keep them in memory. */
  storageKey: string | null;
  /** False where the reader cannot comment: no selection offer appears. */
  enabled: boolean;
  /** A turn is being sent: staged comments and the composer are fenced. */
  busy: boolean;
  /** The transcript whose answers can be selected. */
  transcript: RefObject<HTMLElement | null>;
  /** The message field focus returns to. */
  message: RefObject<HTMLTextAreaElement | null>;
  onError: (message: string | null) => void;
}) {
  const panelId = useId();
  const [annotations, setAnnotations] = useState<StagedChatAnnotation[]>(() =>
    readStagedAnnotations(storageKey),
  );
  const [composer, setComposer] = useState<AnswerCommentComposer | null>(null);
  const [comment, setComment] = useState("");
  const [viewport, setViewport] = useState<ChatAnnotationViewportMetrics | null>(null);
  const [open, setOpen] = useState(false);
  // A finished selection inside an answer offers Comment; the composer opens
  // only when the reader asks, so the platform's Copy stays usable.
  const [offer, setOffer] = useState<{
    range: Range;
    position: ChatAnnotationComposerPosition;
  } | null>(null);
  const commentRef = useRef<HTMLTextAreaElement | null>(null);
  const selectionRef = useRef<HTMLTextAreaElement | null>(null);
  const composerRef = useRef<HTMLFormElement | null>(null);
  const offerRef = useRef<HTMLButtonElement | null>(null);
  const originRef = useRef<HTMLElement | null>(null);
  /** Clears a selection made outside the answer text (an inline artifact's) on close. */
  const externalSelectionClear = useRef<(() => void) | null>(null);
  const composerOpen = composer !== null;
  const composerOpenRef = useRef(composerOpen);
  composerOpenRef.current = composerOpen;

  useEffect(() => writeStagedAnnotations(storageKey, annotations), [annotations, storageKey]);

  useEffect(() => {
    if (!composerOpen) {
      setViewport(null);
      return;
    }
    const visual = window.visualViewport;
    const update = () => {
      setViewport(
        chatAnnotationViewportMetrics(
          { width: window.innerWidth, height: window.innerHeight },
          visual,
        ),
      );
    };
    update();
    visual?.addEventListener("resize", update);
    visual?.addEventListener("scroll", update);
    window.addEventListener("resize", update);
    return () => {
      visual?.removeEventListener("resize", update);
      visual?.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, [composerOpen]);

  useLayoutEffect(() => {
    const form = composerRef.current;
    if (!form || !composer || !viewport) return;
    const update = () => {
      const rect = form.getBoundingClientRect();
      const position = chatAnnotationComposerPosition(composer.anchor, viewport, rect);
      setComposer((current) => {
        if (
          !current ||
          current.anchor !== composer.anchor ||
          (current.position?.left === position.left && current.position.top === position.top)
        )
          return current;
        return { ...current, position };
      });
    };
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(form);
    return () => observer.disconnect();
  }, [composer, viewport]);

  // The composer stays hidden until it is placed, and a hidden field cannot take
  // focus, so the step's field is focused once the composer becomes visible.
  const composerPlaced = Boolean(composer?.position);
  const composerStep = composer?.step;
  useEffect(() => {
    if (!composerPlaced) return;
    if (composerRef.current?.contains(document.activeElement)) return;
    const field = composerStep === "select" ? selectionRef.current : commentRef.current;
    field?.focus();
    if (composerStep === "select") field?.setSelectionRange(0, 0);
  }, [composerPlaced, composerStep]);

  // The pointer often lifts outside the answer that was swept, so the release is
  // observed on the document and the answer is resolved from the selection itself.
  // Touch selection (long press, dragged handles) settles without a pointerup, so
  // a selection change made while no pointer is down refreshes the offer too.
  useEffect(() => {
    if (!enabled) {
      setOffer(null);
      return;
    }
    let pointerDown = false;
    const refresh = () => {
      // The composer re-selects the staged text; that is not a new offer.
      if (composerOpenRef.current) return setOffer(null);
      const range = annotatableAnswerSelectionRange(window.getSelection(), transcript.current);
      const rects = range?.getClientRects();
      const firstLine = rects?.item(0) ?? range?.getBoundingClientRect();
      if (!range || !firstLine) {
        setOffer(null);
        return;
      }
      const button = offerRef.current?.getBoundingClientRect();
      const metrics = chatAnnotationViewportMetrics(
        { width: window.innerWidth, height: window.innerHeight },
        window.visualViewport,
      );
      setOffer({
        range,
        position: chatSelectionCommentPosition(
          { firstLine, bottom: range.getBoundingClientRect().bottom },
          metrics,
          button?.width ? button : { width: 96, height: 32 },
        ),
      });
    };
    const insideOwnControls = (target: EventTarget | null) =>
      target instanceof Node &&
      Boolean(composerRef.current?.contains(target) || offerRef.current?.contains(target));
    const onPointerDown = (event: PointerEvent) => {
      if (!insideOwnControls(event.target)) pointerDown = true;
    };
    const onPointerUp = (event: PointerEvent) => {
      pointerDown = false;
      // Clicks inside the open composer or on Comment must not restart the offer.
      if (insideOwnControls(event.target)) return;
      refresh();
    };
    const onPointerCancel = () => {
      pointerDown = false;
    };
    const onSelectionChange = () => {
      if (!pointerDown) refresh();
    };
    const onViewportChange = () => {
      if (!pointerDown) refresh();
    };
    // Capture phase: a release over a window resize corner stops propagation.
    document.addEventListener("pointerdown", onPointerDown, { capture: true });
    document.addEventListener("pointerup", onPointerUp, { capture: true });
    document.addEventListener("pointercancel", onPointerCancel, { capture: true });
    document.addEventListener("selectionchange", onSelectionChange);
    document.addEventListener("scroll", onViewportChange, { capture: true, passive: true });
    // A resize, rotation, or soft keyboard moves the selection under a fixed offer.
    const visual = window.visualViewport;
    window.addEventListener("resize", onViewportChange);
    visual?.addEventListener("resize", onViewportChange);
    visual?.addEventListener("scroll", onViewportChange);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, { capture: true });
      document.removeEventListener("pointerup", onPointerUp, { capture: true });
      document.removeEventListener("pointercancel", onPointerCancel, { capture: true });
      document.removeEventListener("selectionchange", onSelectionChange);
      document.removeEventListener("scroll", onViewportChange, { capture: true });
      window.removeEventListener("resize", onViewportChange);
      visual?.removeEventListener("resize", onViewportChange);
      visual?.removeEventListener("scroll", onViewportChange);
    };
  }, [enabled, transcript]);

  const focusComment = () => window.requestAnimationFrame(() => commentRef.current?.focus());

  /** False, with the reason shown, once a turn holds as many comments as it can. */
  const roomForAnother = () => {
    if (annotations.length < MAX_CHAT_ANNOTATIONS) return true;
    onError(`A turn can include at most ${MAX_CHAT_ANNOTATIONS} annotations.`);
    return false;
  };

  /** Opens the composer on `next`; focus returns to `origin` when it is dismissed. */
  const openComposer = (
    next: AnswerCommentComposer,
    { draft = "", origin = null }: { draft?: string; origin?: HTMLElement | null } = {},
  ) => {
    originRef.current = origin;
    setComment(draft);
    onError(null);
    setComposer(next);
  };

  const openFromSelection = (range: Range) => {
    if (busy) return;
    const selectedText = range.toString().trim();
    if (!selectedText) return;
    if (selectedText.length > MAX_CHAT_ANNOTATION_TEXT_LENGTH) {
      onError(
        `Select at most ${MAX_CHAT_ANNOTATION_TEXT_LENGTH.toLocaleString()} characters for one comment.`,
      );
      return;
    }
    if (!roomForAnother()) return;
    // Show the reader the text that will be staged, not the overshoot.
    const selection = window.getSelection();
    if (selection) {
      selection.removeAllRanges();
      selection.addRange(range);
    }
    const rects = range.getClientRects();
    const rect = rects.item(rects.length - 1) ?? range.getBoundingClientRect();
    openComposer({
      step: "comment",
      selectedText,
      anchor: { left: rect.left, right: rect.right, top: rect.top },
      position: null,
    });
    focusComment();
  };

  const openFromKeyboard = (answer: HTMLElement, origin: HTMLElement) => {
    if (busy) return;
    if (!roomForAnother()) return;
    const answerText = answer.innerText.trim();
    if (!answerText) return;
    const rect = origin.getBoundingClientRect();
    openComposer(
      {
        step: "select",
        answerText,
        selectedText: "",
        anchor: { left: rect.left, right: rect.right, top: rect.top },
        position: null,
      },
      { origin },
    );
    window.requestAnimationFrame(() => {
      selectionRef.current?.focus();
      selectionRef.current?.setSelectionRange(0, 0);
    });
  };

  const updateKeyboardSelection = (control: HTMLTextAreaElement) => {
    const selectedText = chatAnnotationTextControlSelection(control);
    setComposer((current) => (current?.step === "select" ? { ...current, selectedText } : current));
  };

  const continueKeyboard = () => {
    if (busy) return;
    if (composer?.step !== "select") return;
    const selectedText = composer.selectedText;
    if (!selectedText || selectedText.length > MAX_CHAT_ANNOTATION_TEXT_LENGTH) return;
    setComposer({ step: "comment", selectedText, anchor: composer.anchor, position: null });
    focusComment();
  };

  const dismiss = (returnFocus: boolean) => {
    const origin = originRef.current;
    originRef.current = null;
    externalSelectionClear.current?.();
    externalSelectionClear.current = null;
    setComposer(null);
    setComment("");
    if (returnFocus) {
      window.requestAnimationFrame(() => (origin ?? message.current)?.focus());
    }
  };

  const stage = () => {
    if (busy) return;
    if (composer?.step !== "comment") return;
    const text = comment.trim();
    if (!text) return;
    const artifact = composer.artifact;
    setAnnotations((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        selectedText: composer.selectedText,
        comment: text,
        ...(artifact ? { artifact } : {}),
      },
    ]);
    setOpen(false);
    dismiss(false);
    window.requestAnimationFrame(() => message.current?.focus());
  };

  const update = (id: string, next: string) => {
    if (busy) return;
    setAnnotations((current) =>
      current.map((annotation) =>
        annotation.id === id ? { ...annotation, comment: next } : annotation,
      ),
    );
    onError(null);
  };

  const remove = (id: string) => {
    if (busy) return;
    setAnnotations((current) => {
      const next = current.filter((annotation) => annotation.id !== id);
      if (next.length === 0) setOpen(false);
      return next;
    });
  };

  /** True when every staged comment has text; otherwise opens them with the reason. */
  const requireComplete = () => {
    if (stagedChatAnnotationsAreComplete(annotations)) return true;
    setOpen(true);
    onError("Each staged annotation needs a comment.");
    return false;
  };

  /** After an admitted turn: the turn carries its comments now. */
  const clear = () => {
    setAnnotations([]);
    setOpen(false);
    writeStagedAnnotations(storageKey, []);
  };

  return {
    busy,
    panelId,
    annotations,
    setAnnotations,
    composer,
    comment,
    setComment,
    viewport,
    open,
    setOpen,
    offer,
    setOffer,
    complete: stagedChatAnnotationsAreComplete(annotations),
    refs: { comment: commentRef, selection: selectionRef, composer: composerRef, offer: offerRef },
    externalSelectionClear,
    focusComment,
    roomForAnother,
    openComposer,
    openFromSelection,
    openFromKeyboard,
    updateKeyboardSelection,
    continueKeyboard,
    dismiss,
    stage,
    update,
    remove,
    requireComplete,
    clear,
  };
}

export type AnswerComments = ReturnType<typeof useAnswerComments>;

/** The keyboard way in: opens the composer on the answer this button follows. */
export function AnswerCommentButton({ comments }: { comments: AnswerComments }) {
  return (
    <button
      className="chat-answer-annotation-button"
      type="button"
      aria-label="Comment on this answer"
      disabled={comments.busy}
      onClick={(event) => {
        const answer = event.currentTarget.parentElement?.querySelector<HTMLElement>(
          ".chat-annotatable-answer",
        );
        if (answer) comments.openFromKeyboard(answer, event.currentTarget);
      }}
    >
      <MessageCirclePlus size={12} /> Comment
    </button>
  );
}

/** The staged comments' count above the composer, opening an editable list of them. */
export function StagedAnswerComments({ comments }: { comments: AnswerComments }) {
  const { annotations, open, setOpen, panelId, busy } = comments;
  if (!annotations.length) return null;
  return (
    <>
      <div className="chat-annotation-summary">
        <button
          className={`chat-annotation-count${open ? " is-open" : ""}`}
          type="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((value) => !value)}
        >
          <MessageCircle size={12} /> {annotations.length} annotation
          {annotations.length === 1 ? "" : "s"}
        </button>
      </div>
      {open && (
        <section className="chat-annotation-review" id={panelId} aria-label="Staged annotations">
          <header>
            <strong>Annotations</strong>
            <button
              type="button"
              className="icon-button"
              aria-label="Close annotations"
              onClick={() => setOpen(false)}
            >
              <X size={14} />
            </button>
          </header>
          <div className="chat-annotation-review-list">
            {annotations.map((annotation, index) => (
              <article key={annotation.id}>
                <blockquote>{annotation.selectedText}</blockquote>
                {annotation.artifact && <small>In {annotation.artifact.name}</small>}
                <textarea
                  aria-label={`Comment for annotation ${index + 1}`}
                  disabled={busy}
                  maxLength={MAX_CHAT_ANNOTATION_COMMENT_LENGTH}
                  value={annotation.comment}
                  onChange={(event) => comments.update(annotation.id, event.target.value)}
                />
                <button
                  type="button"
                  aria-label={`Remove annotation ${index + 1}`}
                  disabled={busy}
                  onClick={() => comments.remove(annotation.id)}
                >
                  <X size={12} /> Remove
                </button>
              </article>
            ))}
          </div>
        </section>
      )}
    </>
  );
}

/** The floating Comment offer over a selection, and the comment composer it opens. */
export function AnswerCommentLayers({
  comments,
  mode,
}: {
  comments: AnswerComments;
  /** The conversation mode whose colours the layers take. */
  mode: string;
}) {
  const { offer, composer, viewport, busy, refs } = comments;
  if (typeof document === "undefined") return null;
  const submit = () => {
    if (composer?.step === "select") comments.continueKeyboard();
    else comments.stage();
  };
  return (
    <>
      {offer && !composer
        ? createPortal(
            <button
              ref={refs.offer}
              type="button"
              className="chat-selection-comment"
              data-mode={mode}
              style={{ left: offer.position.left, top: offer.position.top }}
              disabled={busy}
              // Keep the selection while pressing, so the staged text is what was shown.
              onPointerDown={(event) => event.preventDefault()}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                const { range } = offer;
                comments.setOffer(null);
                comments.openFromSelection(range);
              }}
            >
              <MessageCirclePlus size={13} /> Comment
            </button>,
            document.body,
          )
        : null}
      {composer
        ? createPortal(
            <form
              ref={refs.composer}
              className="chat-annotation-composer"
              data-mode={mode}
              aria-label={composer.step === "select" ? "Select answer text" : "Add annotation"}
              style={
                {
                  left: composer.position?.left,
                  top: composer.position?.top,
                  visibility: composer.position ? undefined : "hidden",
                  ...(viewport
                    ? {
                        "--chat-annotation-viewport-left": `${viewport.left}px`,
                        "--chat-annotation-viewport-top": `${viewport.top}px`,
                        "--chat-annotation-viewport-width": `${viewport.width}px`,
                        "--chat-annotation-viewport-height": `${viewport.height}px`,
                        "--chat-annotation-viewport-right": `${viewport.right}px`,
                        "--chat-annotation-viewport-bottom": `${viewport.bottom}px`,
                      }
                    : {}),
                } as CSSProperties
              }
              onSubmit={(event) => {
                event.preventDefault();
                if (busy) return;
                submit();
              }}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  comments.dismiss(true);
                }
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                  event.preventDefault();
                  submit();
                }
              }}
            >
              <header>
                <strong>{composer.step === "select" ? "Select text" : "Add annotation"}</strong>
                <button
                  className="icon-button"
                  type="button"
                  aria-label="Cancel annotation"
                  disabled={busy}
                  onClick={() => comments.dismiss(true)}
                >
                  <X size={14} />
                </button>
              </header>
              {composer.step === "select" ? (
                <>
                  <textarea
                    className="chat-annotation-source"
                    ref={refs.selection}
                    aria-label="Select answer text"
                    disabled={busy}
                    readOnly
                    defaultValue={composer.answerText}
                    onSelect={(event) => comments.updateKeyboardSelection(event.currentTarget)}
                  />
                  {composer.selectedText.length > MAX_CHAT_ANNOTATION_TEXT_LENGTH && (
                    <strong className="chat-annotation-error" role="alert">
                      Select at most {MAX_CHAT_ANNOTATION_TEXT_LENGTH.toLocaleString()} characters.
                    </strong>
                  )}
                  <button
                    className="button compact primary"
                    type="submit"
                    disabled={
                      busy ||
                      !composer.selectedText ||
                      composer.selectedText.length > MAX_CHAT_ANNOTATION_TEXT_LENGTH
                    }
                  >
                    Comment on selection
                  </button>
                </>
              ) : (
                <>
                  <blockquote>{composer.selectedText}</blockquote>
                  <textarea
                    ref={refs.comment}
                    aria-label="Comment"
                    disabled={busy}
                    maxLength={MAX_CHAT_ANNOTATION_COMMENT_LENGTH}
                    value={comments.comment}
                    onChange={(event) => comments.setComment(event.target.value)}
                    onKeyDown={(event) => {
                      // Plain Enter only: Shift+Enter inserts a newline and the
                      // form's own modifier+Enter shortcut stages the comment.
                      if (event.key !== "Enter" || event.shiftKey || event.ctrlKey || event.metaKey)
                        return;
                      event.preventDefault();
                      if (busy || !comments.comment.trim()) return;
                      event.currentTarget.form?.requestSubmit();
                    }}
                  />
                  <button
                    className="button compact primary"
                    type="submit"
                    disabled={busy || !comments.comment.trim()}
                  >
                    Add comment
                  </button>
                </>
              )}
            </form>,
            document.body,
          )
        : null}
    </>
  );
}
