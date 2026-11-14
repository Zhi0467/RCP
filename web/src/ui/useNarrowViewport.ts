import { useEffect, useState } from "react";

/** The phone width. Stylesheets write the same number in their phone queries. */
export const PHONE_MAX_WIDTH_PX = 560;
/** The tablet width. Stylesheets write the same number in their tablet queries. */
export const TABLET_MAX_WIDTH_PX = 920;

export function useNarrowViewport(maxWidthPx = PHONE_MAX_WIDTH_PX): boolean {
  const query = `(max-width: ${maxWidthPx}px)`;
  const [narrow, setNarrow] = useState(
    () => typeof window !== "undefined" && window.matchMedia(query).matches,
  );
  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setNarrow(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return narrow;
}
