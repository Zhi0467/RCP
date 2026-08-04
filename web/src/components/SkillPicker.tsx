import { X } from "lucide-react";
import { useEffect, useState } from "react";
import {
  addSkillSelection,
  clearSkillTrigger,
  filterSkillCatalog,
  hasSkillSelection,
  moveSkillHighlight,
  readSkillTrigger,
  removeSkillSelection,
  selectedSkillRefs,
} from "../skillPicker";
import type { SkillCatalogEntry, SkillDefaults, SkillKind } from "../types";

interface Options {
  catalog: SkillCatalogEntry[];
  defaults: SkillDefaults;
  message: string;
  setMessage: (next: string) => void;
}

/**
 * The `/`-dropdown controller shared by chat and paper coaching.
 *
 * `handleKeyDown` belongs to the composer's own textarea and must run before
 * its send shortcut, so an open dropdown claims the arrows, Enter, and Escape
 * instead of sending the turn.
 */
export function useSkillPicker({ catalog, defaults, message, setMessage }: Options) {
  const [selection, setSelection] = useState<SkillDefaults>(defaults);
  const [query, setQuery] = useState<string | null>(null);
  const [highlight, setHighlight] = useState(0);
  const entries = query === null ? [] : filterSkillCatalog(catalog, query);
  const open = entries.length > 0;

  useEffect(() => {
    setHighlight(0);
  }, [query]);

  const close = () => setQuery(null);

  const reset = () => {
    setSelection(defaults);
    close();
  };

  /** Track the trigger word as the composer text changes. */
  const readMessage = (next: string) => setQuery(readSkillTrigger(next));

  const choose = (entry: SkillCatalogEntry) => {
    setMessage(clearSkillTrigger(message));
    setSelection((current) => addSkillSelection(current, entry));
    close();
  };

  const handleKeyDown = (event: React.KeyboardEvent): boolean => {
    if (!open) return false;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setHighlight((current) =>
        moveSkillHighlight(current, entries.length, event.key === "ArrowDown" ? 1 : -1),
      );
      return true;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      choose(entries[Math.min(highlight, entries.length - 1)]);
      return true;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return true;
    }
    return false;
  };

  return {
    selection,
    setSelection,
    reset,
    readMessage,
    handleKeyDown,
    props: {
      catalog,
      selection,
      entries,
      highlight: Math.min(highlight, Math.max(entries.length - 1, 0)),
      onHighlight: setHighlight,
      onChoose: choose,
      onRemove: (kind: SkillKind, id: string) =>
        setSelection((current) => removeSkillSelection(current, kind, id)),
    },
  };
}

export type SkillPickerProps = ReturnType<typeof useSkillPicker>["props"];

export function SkillPicker({
  catalog,
  selection,
  entries,
  highlight,
  onHighlight,
  onChoose,
  onRemove,
}: SkillPickerProps) {
  const label = (kind: SkillKind, id: string) =>
    catalog.find((item) => item.kind === kind && item.id === id)?.label ?? id;

  return (
    <>
      {hasSkillSelection(selection) && (
        <div className="chat-skill-chips" aria-label="Skills selected for this turn">
          {selectedSkillRefs(selection).map(([kind, id]) => (
            <span className="chat-skill-chip" key={`${kind}:${id}`}>
              {label(kind, id)}
              <button
                type="button"
                aria-label={`Remove ${label(kind, id)}`}
                onClick={() => onRemove(kind, id)}
              >
                <X size={11} />
              </button>
            </span>
          ))}
        </div>
      )}
      {entries.length > 0 && (
        <div className="chat-skill-menu" role="listbox" aria-label="Select a skill or workflow">
          {entries.map((item, index) => (
            <button
              type="button"
              role="option"
              aria-selected={index === highlight}
              className={index === highlight ? "highlighted" : undefined}
              key={`${item.kind}:${item.id}`}
              onMouseDown={(event) => event.preventDefault()}
              onMouseEnter={() => onHighlight(index)}
              onClick={() => onChoose(item)}
            >
              <span>
                <strong>{item.label}</strong>
                <small>
                  {item.kind} · {item.description}
                </small>
              </span>
            </button>
          ))}
        </div>
      )}
    </>
  );
}
