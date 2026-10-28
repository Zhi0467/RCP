import { useEffect, useRef, useState } from "react";

/** Large previews wait for idle; small previews also update during continuous typing.
 * A new file shows at once.
 * An inactive (hidden) preview keeps its text until shown again. */
export function usePreviewText(text: string, fileId: string, active: boolean) {
  const [preview, setPreview] = useState({ fileId, text });
  const pendingSince = useRef<number | null>(null);
  if (preview.fileId !== fileId) setPreview({ fileId, text });
  useEffect(() => {
    if (text === preview.text) {
      pendingSince.current = null;
      return;
    }
    if (!active) return;
    const now = Date.now();
    pendingSince.current ??= now;
    const timer = setTimeout(
      () => {
        pendingSince.current = null;
        setPreview({ fileId, text });
      },
      text.length > 200_000
        ? 1000
        : Math.min(250, Math.max(0, 1000 - (now - pendingSince.current))),
    );
    return () => clearTimeout(timer);
  }, [text, preview.text, fileId, active]);
  return preview.fileId === fileId ? preview.text : text;
}
