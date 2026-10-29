import { useEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { Download, Ellipsis, FilePlus, FolderPlus, Pencil, Trash2, Upload } from "lucide-react";
import type { PaperFile } from "../core/types";

/** The root, one folder (`empty` when only this browser knows it), or one file. */
export type PaperTreeTarget =
  | { kind: "root" }
  | { kind: "folder"; path: string; empty: boolean }
  | { kind: "file"; file: PaperFile };
export type PaperTreeAction =
  "new-file" | "new-folder" | "upload" | "rename" | "download" | "delete";

const ACTIONS: Record<PaperTreeTarget["kind"], PaperTreeAction[]> = {
  root: ["new-file", "new-folder", "upload"],
  folder: ["new-file", "new-folder", "upload", "rename", "download", "delete"],
  file: ["rename", "download", "delete"],
};
const ITEMS: Record<PaperTreeAction, [string, typeof Pencil]> = {
  "new-file": ["New file", FilePlus],
  "new-folder": ["New folder", FolderPlus],
  upload: ["Upload", Upload],
  rename: ["Rename", Pencil],
  download: ["Download", Download],
  delete: ["Delete", Trash2],
};
const ITEM_HEIGHT = 44;

/** One ⋯ menu for every tree row and the Files header. The list renders in the body,
 * fixed to its button, so the scrolling tree never clips it. */
export function PaperTreeMenu({
  label,
  target,
  disabled,
  onAction,
}: {
  label: string;
  target: PaperTreeTarget;
  disabled?: boolean;
  onAction: (action: PaperTreeAction, target: PaperTreeTarget) => void;
}) {
  const [place, setPlace] = useState<CSSProperties | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const actions = ACTIONS[target.kind];
  useEffect(() => {
    if (!place) return;
    const dismiss = () => setPlace(null);
    const outside = (event: Event) => {
      const node = event.target as Node;
      if (!list.current?.contains(node) && !button.current?.contains(node)) dismiss();
    };
    // Only a scroll that moves the button strands the fixed list; the editor's own do not.
    const scrolled = (event: Event) => {
      if (event.target instanceof Node && event.target.contains(button.current)) dismiss();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      dismiss();
      button.current?.focus();
    };
    list.current?.querySelector("button")?.focus();
    window.addEventListener("pointerdown", outside);
    window.addEventListener("keydown", escape);
    window.addEventListener("resize", dismiss);
    window.addEventListener("scroll", scrolled, true);
    return () => {
      window.removeEventListener("pointerdown", outside);
      window.removeEventListener("keydown", escape);
      window.removeEventListener("resize", dismiss);
      window.removeEventListener("scroll", scrolled, true);
    };
  }, [place]);
  const toggle = () => {
    if (place) return setPlace(null);
    const rect = button.current!.getBoundingClientRect();
    const height = actions.length * ITEM_HEIGHT + 10;
    setPlace({
      left: Math.max(8, Math.min(rect.left, innerWidth - 188)),
      ...(rect.bottom + height + 8 > innerHeight
        ? { bottom: innerHeight - rect.top + 4 }
        : { top: rect.bottom + 4 }),
    });
  };
  return (
    <>
      <button
        ref={button}
        type="button"
        className={target.kind === "root" ? "icon-button" : "paper-tree-menu-button"}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={Boolean(place)}
        disabled={disabled}
        onClick={toggle}
      >
        <Ellipsis size={15} />
      </button>
      {place &&
        createPortal(
          <div ref={list} className="paper-tree-menu" role="menu" aria-label={label} style={place}>
            {actions.map((action) => {
              const [name, Icon] = ITEMS[action];
              return (
                <button
                  key={action}
                  type="button"
                  role="menuitem"
                  disabled={action === "download" && target.kind === "folder" && target.empty}
                  onClick={() => {
                    setPlace(null);
                    onAction(action, target);
                  }}
                >
                  <Icon size={14} />
                  {name}
                </button>
              );
            })}
          </div>,
          document.body,
        )}
    </>
  );
}
