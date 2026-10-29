import { useEffect, useState, type CSSProperties } from "react";

const KEY = "rcp:paper-editor-width";
const MIN_EDITOR = 280;
const MIN_RIGHT = 320;

function storedWidth(): number | null {
  try {
    const value = Number(localStorage.getItem(KEY));
    return value > 0 ? value : null;
  } catch {
    return null;
  }
}

/** The editor's width beside Preview/Coach: dragged by the viewer, kept per browser, and
 * clamped so both panes keep a usable width as the window changes. */
export function usePaperSplit() {
  // State, not refs: the panes mount after the workspace's first render.
  const [editor, setEditor] = useState<HTMLElement | null>(null);
  const [right, setRight] = useState<HTMLElement | null>(null);
  // The width the two panes share; 0 until measured, and while they stack on a phone.
  const [room, setRoom] = useState(0);
  const [chosen, setChosen] = useState(storedWidth);
  useEffect(() => {
    const measure = () => {
      const left = editor?.getBoundingClientRect();
      const end = right?.getBoundingClientRect();
      setRoom(left && end && end.left > left.right ? left.width + end.width : 0);
    };
    const observer = new ResizeObserver(measure);
    for (const pane of [editor, right]) if (pane) observer.observe(pane);
    measure();
    return () => observer.disconnect();
  }, [editor, right]);
  const maximum = Math.max(MIN_EDITOR, room - MIN_RIGHT);
  const width = Math.min(maximum, Math.max(MIN_EDITOR, chosen ?? room / 2));
  const resize = (next: number) => {
    setChosen(next);
    try {
      localStorage.setItem(KEY, String(Math.round(next)));
    } catch {
      /* The width is a per-viewer convenience. */
    }
  };
  return {
    editor: setEditor,
    right: setRight,
    style: (room && chosen !== null
      ? { "--paper-editor-width": `${width}px` }
      : {}) as CSSProperties,
    handle: room
      ? {
          width,
          minimum: MIN_EDITOR,
          maximum,
          widthAt: (clientX: number) => {
            const left = editor?.getBoundingClientRect().left;
            return left === undefined ? null : clientX - left;
          },
          onResize: resize,
        }
      : null,
  };
}
