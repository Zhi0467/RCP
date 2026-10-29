import { useEffect, useState } from "react";

/** A first click arms a destructive action; this long without a second click disarms it. */
const ARMED_MS = 4000;

/** Two-click confirmation: the first click arms, the second acts, blur or time disarms. */
export function useArmedConfirm() {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), ARMED_MS);
    return () => window.clearTimeout(timer);
  }, [armed]);
  return { armed, arm: () => setArmed(true), disarm: () => setArmed(false) };
}
