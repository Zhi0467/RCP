import { LoaderCircle, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

/** A first click arms Delete; this long without a second click disarms it. */
const ARMED_MS = 4000;

/**
 * Two-click Delete: the first click arms it and shows "Delete" beside the icon,
 * the second confirms; blur or time disarms. A failure marks it with its message.
 */
export function ArmedDeleteButton({
  label,
  className,
  iconSize = 14,
  busy = false,
  disabled = false,
  failure = null,
  onConfirm,
  ...data
}: {
  label: string;
  className: string;
  iconSize?: number;
  busy?: boolean;
  disabled?: boolean;
  failure?: string | null;
  onConfirm: () => void;
  [attribute: `data-${string}`]: string;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), ARMED_MS);
    return () => window.clearTimeout(timer);
  }, [armed]);
  const failed = !armed && failure !== null;
  return (
    <button
      {...data}
      type="button"
      className={`${className}${armed ? " armed-delete" : failed ? " failed" : ""}`}
      aria-label={armed ? "Confirm delete" : label}
      aria-invalid={failed || undefined}
      title={failed ? failure : undefined}
      disabled={disabled || busy}
      onBlur={() => setArmed(false)}
      onClick={() => {
        setArmed(!armed);
        if (armed) onConfirm();
      }}
    >
      {busy ? (
        <LoaderCircle className="spin" size={iconSize} />
      ) : (
        <Trash2 size={iconSize} aria-hidden="true" />
      )}
      {armed && "Delete"}
    </button>
  );
}
