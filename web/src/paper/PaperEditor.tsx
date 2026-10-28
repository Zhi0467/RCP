import { useEffect, useMemo, useRef, useState } from "react";
import { Compartment, EditorState } from "@codemirror/state";
import { Decoration, EditorView, keymap, lineNumbers, WidgetType } from "@codemirror/view";
import { defaultKeymap } from "@codemirror/commands";
import { defaultHighlightStyle, StreamLanguage, syntaxHighlighting } from "@codemirror/language";
import { markdown } from "@codemirror/lang-markdown";
import { stex } from "@codemirror/legacy-modes/mode/stex";
import { typst_lezer } from "codemirror-lang-typst/lezer";
import { yCollab, yUndoManagerKeymap } from "y-codemirror.next";
import { Check, ChevronLeft, ChevronRight, X } from "lucide-react";
import type { PaperDocument } from "./paperDocuments";
import type { PaperChange } from "./PaperChanges";

export function paperAnchorMatches(change: PaperChange, text: string, region: string) {
  return change.preview.removed.length
    ? region === change.preview.removed.join("\n")
    : change.anchorText !== undefined && text === change.anchorText;
}

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

export function PaperEditor({
  owner,
  format,
  changes,
  onDecide,
  focusChange,
}: {
  owner: PaperDocument;
  format: string;
  changes: PaperChange[];
  onDecide: (id: string, action: "accept" | "reject") => void;
  focusChange: PaperChange | null;
}) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const suggestions = useRef(new Compartment());
  const decide = useRef(onDecide);
  decide.current = onDecide;
  const [position, setPosition] = useState({ line: 1, column: 1 });
  const [step, setStep] = useState(0);
  const pending = useMemo(
    () =>
      changes.filter(
        (change) => change.file_id === owner.key.file && change.state === "pending" && change.lines,
      ),
    [changes, owner],
  );
  const { text, error } = owner.getSnapshot();
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
    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: owner.text.toString(),
        extensions: [
          lineNumbers(),
          keymap.of([...yUndoManagerKeymap, ...defaultKeymap]),
          language,
          syntaxHighlighting(defaultHighlightStyle),
          EditorView.lineWrapping,
          EditorView.contentAttributes.of({ "aria-label": "Paper source", spellcheck: "false" }),
          yCollab(owner.text, owner.provider.awareness),
          suggestions.current.of([]),
          EditorView.updateListener.of((update) => {
            if (update.selectionSet || update.docChanged) {
              const head = update.state.selection.main.head;
              const line = update.state.doc.lineAt(head);
              setPosition({ line: line.number, column: head - line.from + 1 });
            }
          }),
        ],
      }),
    });
    view.current = editor;
    return () => {
      view.current = null;
      editor.destroy();
    };
  }, [owner, format]);

  useEffect(() => {
    const editor = view.current;
    if (!editor) return;
    const marks = pending.flatMap((change) => {
      const start = change.lines!.start;
      const end = change.lines!.end;
      const insertion = change.preview.removed.length === 0;
      if (start < 1 || start > editor.state.doc.lines + (insertion ? 1 : 0)) return [];
      const from =
        start > editor.state.doc.lines
          ? editor.state.doc.length
          : editor.state.doc.line(start).from;
      const to = insertion
        ? from
        : editor.state.doc.line(Math.min(Math.max(start, end), editor.state.doc.lines)).to;
      // A moved or edited anchor is never painted as if it were still current.
      if (!paperAnchorMatches(change, text, editor.state.doc.sliceString(from, to))) return [];
      const result = [
        Decoration.widget({
          widget: new SuggestionWidget(change, (id, action) => decide.current(id, action)),
          side: 1,
        }).range(to),
      ];
      if (to > from && change.preview.removed.length)
        result.push(Decoration.mark({ class: "paper-deletion" }).range(from, to));
      return result;
    });
    editor.dispatch({
      effects: suggestions.current.reconfigure(
        EditorView.decorations.of(Decoration.set(marks, true)),
      ),
    });
  }, [pending, owner, text]);

  const locate = (change: PaperChange | undefined) => {
    const editor = view.current;
    if (!editor || !change?.lines) return;
    const line = editor.state.doc.line(
      Math.min(editor.state.doc.lines, Math.max(1, change.lines.start)),
    );
    editor.dispatch({
      selection: { anchor: line.from },
      effects: EditorView.scrollIntoView(line.from, { y: "center" }),
    });
    editor.focus();
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
    locate(focusChange);
    located.current = { owner, change: focusChange };
  }, [focusChange, owner, text]);
  const words = owner.getSnapshot().text.trim().split(/\s+/).filter(Boolean).length;
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
