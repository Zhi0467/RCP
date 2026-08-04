import { useEffect, useState } from "react";
import {
  EMPTY_SKILL_SELECTION,
  addSkillSelection,
  filterSkillCatalogToDefaults,
  filterSkillCatalog,
  moveSkillHighlight,
  readSkillTrigger,
} from "../skillPicker";
import type { SkillCatalogEntry, SkillDefaults } from "../types";

interface Options {
  catalog: SkillCatalogEntry[];
  defaults: SkillDefaults;
}

/**
 * The `/`-dropdown controller shared by chat and paper coaching.
 *
 * `handleKeyDown` belongs to the composer's own textarea and must run before
 * its send shortcut, so an open dropdown claims the arrows, Enter, and Escape
 * instead of sending the turn.
 */
export function useSkillPicker({ catalog, defaults }: Options) {
  const [selection, setSelection] = useState<SkillDefaults>(EMPTY_SKILL_SELECTION);
  const [query, setQuery] = useState<string | null>(null);
  const [highlight, setHighlight] = useState(0);
  const enabledCatalog = filterSkillCatalogToDefaults(catalog, defaults);
  const entries = query === null ? [] : filterSkillCatalog(enabledCatalog, query);
  const open = entries.length > 0;
  const defaultsKey = `${defaults.workflow_ids.join(",")}|${defaults.skill_ids.join(",")}`;

  useEffect(() => {
    setHighlight(0);
  }, [query]);

  useEffect(() => {
    setSelection(EMPTY_SKILL_SELECTION);
    setQuery(null);
  }, [defaultsKey]);

  const close = () => setQuery(null);

  const reset = () => {
    setSelection(EMPTY_SKILL_SELECTION);
    close();
  };

  /** Track the trigger word as the composer text changes. */
  const readMessage = (next: string) => setQuery(readSkillTrigger(next));

  const choose = (entry: SkillCatalogEntry) => {
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
    },
  };
}

export type SkillPickerProps = ReturnType<typeof useSkillPicker>["props"];

export function SkillPicker({ entries, highlight, onHighlight, onChoose }: SkillPickerProps) {
  return (
    <>
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
