import { RotateCcw } from "lucide-react";
export function AgentWindowResend({
  error,
  disabled = false,
  onResend,
}: {
  error: string;
  disabled?: boolean;
  onResend: () => void;
}) {
  return (
    <button
      type="button"
      className="icon-button chat-turn-resend"
      aria-label="Resend"
      title={error}
      disabled={disabled}
      onClick={onResend}
    >
      <RotateCcw size={14} />
    </button>
  );
}
