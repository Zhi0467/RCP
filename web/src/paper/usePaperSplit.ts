import { useEffect, useState, type CSSProperties } from "react";
import { useStoredSplit } from "../ui/useStoredSplit";

const MIN_EDITOR = 280;
const MIN_RIGHT = 320;

/** The editor's width beside Preview/Coach: dragged by the viewer, kept per browser, and
 * clamped so both panes keep a usable width as the window changes. */
export function usePaperSplit() {
  // State, not refs: the panes mount after the workspace's first render.
  const [editor, setEditor] = useState<HTMLElement | null>(null);
  const [right, setRight] = useState<HTMLElement | null>(null);
  // The width the two panes share; 0 until measured, and while they stack on a phone.
  const [room, setRoom] = useState(0);
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
  const split = useStoredSplit(
    "rcp:paper-editor-width",
    { minimum: MIN_EDITOR, maximum },
    room / 2,
  );
  return {
    editor: setEditor,
    right: setRight,
    style: (room && split.chosen
      ? { "--paper-editor-width": `${split.width}px` }
      : {}) as CSSProperties,
    handle: room
      ? {
          width: split.width,
          minimum: MIN_EDITOR,
          maximum,
          widthAt: (clientX: number) => {
            const left = editor?.getBoundingClientRect().left;
            return left === undefined ? null : clientX - left;
          },
          onResize: split.resize,
        }
      : null,
  };
}
