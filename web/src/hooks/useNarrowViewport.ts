import { useEffect, useState } from "react";

/** The phone width. Stylesheets write the same number in their phone queries. */
export const PHONE_MAX_WIDTH_PX = 560;
const NARROW_VIEWPORT = `(max-width: ${PHONE_MAX_WIDTH_PX}px)`;

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
