import { useEffect, useMemo, useRef, useState } from "react";
import { Compartment, EditorSelection, EditorState } from "@codemirror/state";
import { Decoration, EditorView, keymap, lineNumbers, WidgetType } from "@codemirror/view";
import { defaultKeymap } from "@codemirror/commands";
import { defaultHighlightStyle, StreamLanguage, syntaxHighlighting } from "@codemirror/language";
import { markdown } from "@codemirror/lang-markdown";
import { stex } from "@codemirror/legacy-modes/mode/stex";
import { typst_lezer } from "codemirror-lang-typst/lezer";
import * as Y from "yjs";
import { yCollab, ySyncAnnotation } from "y-codemirror.next";
import { Check, ChevronLeft, ChevronRight, X } from "lucide-react";
import { anchorPaperChange, resolvePaperAnchor } from "./paperAnchors";
import { paperDocumentKey, type PaperDocument } from "./paperDocuments";
import type { PaperChange } from "./PaperChanges";

class SuggestionWidget extends WidgetType {
  constructor(
    readonly change: PaperChange,
    readonly decide: (id: string, action: "accept" | "reject") => void,
  ) {
    super();
  }
  toDOM() {
    const wrapper = document.createElement("span");
    wrapper.className = "paper-inline-suggestion";
    const insertion = document.createElement("span");
    insertion.className = "paper-insertion";
    insertion.textContent = this.change.preview.added.join("\n");
    wrapper.append(insertion);
    const actions = document.createElement("span");
    actions.className = "paper-suggestion-actions";
    for (const [action, symbol] of [
      ["accept", "✓"],
      ["reject", "×"],
    ] as const) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "icon-button compact";
      button.setAttribute("aria-label", `${action} suggestion`);
      button.textContent = symbol;
      button.onmousedown = (event) => event.preventDefault();
      button.onclick = () => this.decide(this.change.group_id, action);
      actions.append(button);
    }
    wrapper.append(actions);
    return wrapper;
  }
  ignoreEvent() {
    return true;
  }
}

/** Per-document editor memory: a file switch keeps undo, selection and scroll.
 * Keyed by Y.Text, so a rejected document's replacement starts fresh. */
const memories = new WeakMap<
  Y.Text,
  {
    undo: Y.UndoManager;
    anchor?: Y.RelativePosition;
    head?: Y.RelativePosition;
    top?: Y.RelativePosition;
  }
>();
/** The latest memory per file, so a document rebuilt after a backend restart resumes
 * at the same cursor and scroll; its undo history starts fresh with the new document. */
const lastMemories = new Map<string, NonNullable<ReturnType<typeof memories.get>>>();

export function PaperEditor({
  owner,
  format,
  changes,
  onDecide,
  focusChange,
  maxBytes,
}: {
  owner: PaperDocument;
  maxBytes?: number;
  format: string;
  changes: PaperChange[];
  onDecide: (id: string, action: "accept" | "reject") => void;
  focusChange: PaperChange | null;
}) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const suggestions = useRef(new Compartment());
  const readOnly = useRef(new Compartment());
  const byteLimit = useRef(maxBytes);
  byteLimit.current = maxBytes;
  const decide = useRef(onDecide);
  decide.current = onDecide;
  const [position, setPosition] = useState({ line: 1, column: 1 });
  const [step, setStep] = useState(0);
  const report = (state: EditorState) => {
    const head = state.selection.main.head;
    const line = state.doc.lineAt(head);
    setPosition({ line: line.number, column: head - line.from + 1 });
  };
  const pending = useMemo(
    () =>
      changes.filter(
        (change) =>
          change.file_id === owner.key.file && change.state === "pending" && change.current,
      ),
    [changes, owner],
  );
  const anchors = useMemo(
    () => new Map<PaperChange, ReturnType<typeof anchorPaperChange>>(),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the deps reset this anchor cache; the factory needs none of them
    [pending, owner, owner.text],
  );
  const { text, error, closed } = owner.getSnapshot();
  const locked = closed !== null;
  const currentStep = Math.min(step, pending.length - 1);
  const active = pending[currentStep];
  useEffect(() => {
    if (!host.current) return;
    const language =
      format === "markdown"
        ? markdown()
        : format === "latex"
          ? StreamLanguage.define(stex)
          : format === "typst"
            ? typst_lezer()
            : [];
    let memory = memories.get(owner.text);
    if (!memory) {
      memory = {
        ...lastMemories.get(paperDocumentKey(owner.key)),
        undo: new Y.UndoManager(owner.text),
      };
      memories.set(owner.text, memory);
    }
    lastMemories.set(paperDocumentKey(owner.key), memory);
    const saved = memory;
    const relative = (index: number) => Y.createRelativePositionFromTypeIndex(owner.text, index);
    const absolute = (position: Y.RelativePosition | undefined) =>
      position && Y.createAbsolutePositionFromRelativePosition(position, owner.doc)?.index;
    const anchor = absolute(saved.anchor);
    const head = absolute(saved.head);
    const top = absolute(saved.top);
    let failureFlash: Animation | undefined;
    const encoder = new TextEncoder();
    const flashRefusal = () => {
      failureFlash?.cancel();
      failureFlash = host.current?.animate(
        [
          { boxShadow: "inset 3px 0 0 var(--coral)" },
          { boxShadow: "inset 3px 0 0 var(--coral)" },
          { boxShadow: "inset 3px 0 0 transparent" },
        ],
        { duration: 1200 },
      );
    };
    // Undo and redo reach CodeMirror as synced Yjs changes, past the filter below, so
    // bound what they would restore before they touch the shared document.
    const guarded = (stack: "undoStack" | "redoStack", run: () => void) => () => {
      const item = saved.undo[stack].at(-1);
      const limit = byteLimit.current;
      if (item && limit !== undefined) {
        let restored = 0;
        owner.doc.transact((transaction) =>
          Y.iterateDeletedStructs(transaction, item.deletions, (struct) => {
            if (struct instanceof Y.Item && struct.parent === owner.text)
              restored += encoder.encode(struct.content.getContent().join("")).byteLength;
          }),
        );
        if (encoder.encode(owner.text.toString()).byteLength + restored > limit) {
          flashRefusal();
          return true;
        }
      }
      run();
      return true;
    };
    const editor = new EditorView({
      parent: host.current,
      scrollTo: top === undefined ? undefined : EditorView.scrollIntoView(top, { y: "start" }),
      state: EditorState.create({
        doc: owner.text.toString(),
        selection:
          anchor === undefined || head === undefined
            ? undefined
            : EditorSelection.single(anchor, head),
        extensions: [
          lineNumbers(),
          EditorState.changeFilter.of((transaction) => {
            if (
              !transaction.docChanged ||
              transaction.annotation(ySyncAnnotation) ||
              byteLimit.current === undefined ||
              // A UTF-16 unit is at most three UTF-8 bytes: skip the encode far below the limit.
              transaction.newDoc.length * 3 <= byteLimit.current ||
              encoder.encode(transaction.newDoc.toString()).byteLength <= byteLimit.current
            )
              return true;
            flashRefusal();
            return false;
          }),
          keymap.of([
            {
              key: "Mod-z",
              run: guarded("undoStack", () => saved.undo.undo()),
              preventDefault: true,
            },
            {
              key: "Mod-y",
              mac: "Mod-Shift-z",
              run: guarded("redoStack", () => saved.undo.redo()),
              preventDefault: true,
            },
            {
              key: "Mod-Shift-z",
              run: guarded("redoStack", () => saved.undo.redo()),
              preventDefault: true,
            },
            ...defaultKeymap,
          ]),
          language,
          syntaxHighlighting(defaultHighlightStyle),
          EditorView.lineWrapping,
          EditorView.contentAttributes.of({ "aria-label": "Paper source", spellcheck: "false" }),
          yCollab(owner.text, owner.provider.awareness, { undoManager: saved.undo }),
          suggestions.current.of([]),
          readOnly.current.of(EditorState.readOnly.of(owner.getSnapshot().closed !== null)),
          EditorView.domEventObservers({
            scroll(_event, view) {
              saved.top = relative(view.lineBlockAtHeight(view.scrollDOM.scrollTop).from);
            },
          }),
          EditorView.updateListener.of((update) => {
            if (update.selectionSet || update.docChanged) report(update.state);
          }),
        ],
      }),
    });
    view.current = editor;
    report(editor.state);
    return () => {
      const { main } = editor.state.selection;
      saved.anchor = relative(main.anchor);
      saved.head = relative(main.head);
      view.current = null;
      failureFlash?.cancel();
      editor.destroy();
    };
  }, [owner, owner.text, format]);
  useEffect(() => {
    view.current?.dispatch({
      effects: readOnly.current.reconfigure(EditorState.readOnly.of(locked)),
    });
  }, [locked]);

  useEffect(() => {
    const editor = view.current;
    if (!editor) return;
    const marks = pending.flatMap((change) => {
      let anchor = anchors.get(change);
      if (!anchors.has(change)) {
        anchor = anchorPaperChange(change, owner.text);
        // Wait for the first server text, then keep failed matches hidden until
        // the server supplies a refreshed card instead of trying an old offset.
        if (anchor || text || owner.provider.synced) anchors.set(change, anchor);
      }
      const range = anchor && resolvePaperAnchor(anchor, change, owner.text);
      if (!range) return [];
      const { from, to } = range;
      // The proposal sits before a trailing newline, and the deletion mark ends where it
      // starts, so the proposed text is never inside the struck-through range.
      const at = to > from && editor.state.doc.sliceString(to - 1, to) === "\n" ? to - 1 : to;
      const result = [
        Decoration.widget({
          widget: new SuggestionWidget(change, (id, action) => decide.current(id, action)),
          side: 1,
        }).range(at),
      ];
      if (at > from && change.preview.removed.length)
        result.push(Decoration.mark({ class: "paper-deletion" }).range(from, at));
      return result;
    });
    editor.dispatch({
      effects: suggestions.current.reconfigure(
        EditorView.decorations.of(Decoration.set(marks, true)),
      ),
    });
  }, [pending, owner, text, anchors]);

  const locate = (change: PaperChange | undefined) => {
    const editor = view.current;
    if (!editor || !change) return false;
    const anchor = anchors.get(change);
    const range = anchor && resolvePaperAnchor(anchor, change, owner.text);
    if (!range) return false;
    const { from } = range;
    editor.dispatch({
      selection: { anchor: from },
      effects: EditorView.scrollIntoView(from, { y: "center" }),
    });
    editor.focus();
    return true;
  };
  const located = useRef<{ owner: PaperDocument; change: PaperChange } | null>(null);
  useEffect(() => {
    if (
      !focusChange ||
      focusChange.file_id !== owner.key.file ||
      !text ||
      (located.current?.owner === owner && located.current.change === focusChange)
    )
      return;
    if (locate(focusChange)) located.current = { owner, change: focusChange };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- locate is recreated each render; text arriving is the trigger
  }, [focusChange, owner, text]);
  const words = useMemo(() => text.trim().split(/\s+/).filter(Boolean).length, [text]);
  return (
    <>
      <div className={`paper-code paper-well ${error ? "paper-failed" : ""}`} ref={host} />
      <footer className="paper-editor-footer">
        <span className="paper-position">
          Ln {position.line}, Col {position.column}
        </span>
        <span>{words} words</span>
        {active && (
          <div className="paper-suggestion-stepper">
            <span>
              {currentStep + 1} / {pending.length}
            </span>
            <button
              className="icon-button compact"
              aria-label="Previous suggestion"
              onClick={() => {
                const next = (currentStep - 1 + pending.length) % pending.length;
                setStep(next);
                locate(pending[next]);
              }}
            >
              <ChevronLeft size={14} />
            </button>
            <button
              className="icon-button compact"
              aria-label="Next suggestion"
              onClick={() => {
                const next = (currentStep + 1) % pending.length;
                setStep(next);
                locate(pending[next]);
              }}
            >
              <ChevronRight size={14} />
            </button>
            <button
              className="icon-button compact"
              aria-label="Accept suggestion"
              onClick={() => onDecide(active.group_id, "accept")}
            >
              <Check size={14} />
            </button>
            <button
              className="icon-button compact"
              aria-label="Reject suggestion"
              onClick={() => onDecide(active.group_id, "reject")}
            >
              <X size={14} />
            </button>
          </div>
        )}
      </footer>
    </>
  );
}
