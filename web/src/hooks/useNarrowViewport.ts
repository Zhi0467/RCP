import { useEffect, useState } from "react";

const NARROW_VIEWPORT = "(max-width: 560px)";

export function useNarrowViewport(): boolean {
  const [narrow, setNarrow] = useState(
    () => typeof window !== "undefined" && window.matchMedia(NARROW_VIEWPORT).matches,
  );
  useEffect(() => {
    const media = window.matchMedia(NARROW_VIEWPORT);
    const update = () => setNarrow(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return narrow;
}
