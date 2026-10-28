import { useEffect, useRef, useState } from "react";

/** Idle preview updates, with a deadline during continuous typing. */
export function usePreviewText(text: string) {
  const [preview, setPreview] = useState(text);
  const pendingSince = useRef<number | null>(null);
  useEffect(() => {
    if (text === preview) {
      pendingSince.current = null;
      return;
    }
    const now = Date.now();
    pendingSince.current ??= now;
    const timer = setTimeout(
      () => {
        pendingSince.current = null;
        setPreview(text);
      },
      Math.min(250, Math.max(0, 1000 - (now - pendingSince.current))),
    );
    return () => clearTimeout(timer);
  }, [text, preview]);
  return preview;
}
