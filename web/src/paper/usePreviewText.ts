import { useEffect, useRef, useState } from "react";

/** Idle preview updates, with a deadline during continuous typing; a new file shows at once. */
export function usePreviewText(text: string, fileId: string) {
  const [preview, setPreview] = useState({ fileId, text });
  const pendingSince = useRef<number | null>(null);
  if (preview.fileId !== fileId) setPreview({ fileId, text });
  useEffect(() => {
    if (text === preview.text) {
      pendingSince.current = null;
      return;
    }
    const now = Date.now();
    pendingSince.current ??= now;
    const timer = setTimeout(
      () => {
        pendingSince.current = null;
        setPreview({ fileId, text });
      },
      Math.min(250, Math.max(0, 1000 - (now - pendingSince.current))),
    );
    return () => clearTimeout(timer);
  }, [text, preview.text, fileId]);
  return preview.fileId === fileId ? preview.text : text;
}
