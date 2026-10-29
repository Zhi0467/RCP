import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { Download, Ellipsis, FilePlus, FolderPlus, Pencil, Trash2, Upload } from "lucide-react";
import type { PaperFile } from "../core/types";
import { useLayerDismiss } from "../core/useLayerDismiss";

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
/** One ⋯ menu for every tree row and the Files header. The list renders in the body,
 * fixed to its button, so the scrolling tree never clips it. */
export function PaperTreeMenu({
  label,
  target,
  onAction,
}: {
  label: string;
  target: PaperTreeTarget;
  onAction: (action: PaperTreeAction, target: PaperTreeTarget) => void;
}) {
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const [place, setPlace] = useState<CSSProperties | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const actions = ACTIONS[target.kind];
  useLayerDismiss(Boolean(anchor), () => setAnchor(null), [list, button], button);
  // The list opens below its button, or above it when it would run off the bottom.
  useLayoutEffect(() => {
    const menu = list.current;
    if (!anchor || !menu) return setPlace(null);
    const { width, height } = menu.getBoundingClientRect();
    setPlace({
      left: Math.max(8, Math.min(anchor.left, innerWidth - width - 8)),
      top: anchor.bottom + height + 8 > innerHeight ? anchor.top - height - 4 : anchor.bottom + 4,
    });
  }, [anchor]);
  useEffect(() => {
    if (!place) return;
    list.current?.querySelector("button")?.focus();
    const dismiss = () => setAnchor(null);
    // Only a scroll that moves the button strands the fixed list; the editor's own do not.
    const scrolled = (event: Event) => {
      if (event.target instanceof Node && event.target.contains(button.current)) dismiss();
    };
    window.addEventListener("resize", dismiss);
    window.addEventListener("scroll", scrolled, true);
    return () => {
      window.removeEventListener("resize", dismiss);
      window.removeEventListener("scroll", scrolled, true);
    };
  }, [place]);
  const toggle = () => setAnchor(anchor ? null : button.current!.getBoundingClientRect());
  return (
    <>
      <button
        ref={button}
        type="button"
        className={target.kind === "root" ? "icon-button" : "paper-tree-menu-button"}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={Boolean(anchor)}
        onClick={toggle}
      >
        <Ellipsis size={15} />
      </button>
      {anchor &&
        createPortal(
          <div
            ref={list}
            className="paper-tree-menu"
            role="menu"
            aria-label={label}
            style={place ?? { visibility: "hidden" }}
          >
            {actions.map((action) => {
              const [name, Icon] = ITEMS[action];
              return (
                <button
                  key={action}
                  type="button"
                  role="menuitem"
                  disabled={action === "download" && target.kind === "folder" && target.empty}
                  onClick={() => {
                    setAnchor(null);
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
