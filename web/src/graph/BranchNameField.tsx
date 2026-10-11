import { GitBranch } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { ApiError } from "../core/api";
import { branchNameProblem } from "../core/branchNames";

const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

type LaunchMode = "auto_research" | "experiment_loop";
const FALLBACK_PREFIX: Record<LaunchMode, string> = {
  auto_research: "research-",
  experiment_loop: "experiment-",
};

/** The name a blank field gets from the server, for today's local date. */
export function branchNamePlaceholder(mode: LaunchMode, now: Date = new Date()): string {
  return `${FALLBACK_PREFIX[mode]}${MONTHS[now.getMonth()]}-${String(now.getDate()).padStart(2, "0")}`;
}

/** The server's branch-name refusal code, when a start failed on the name. */
export function branchNameRefusal(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  if (error.status === 409 && error.code === "branch_name_taken") return error.code;
  if (error.status === 422 && error.code === "branch_name_invalid") return error.code;
  return null;
}

/** What a launch sends: blank lets the server assign the date fallback. */
export function branchNameRequest(value: string): string | null {
  return value.trim() || null;
}

export function branchNameValid(value: string): boolean {
  return branchNameProblem(value.trim()) === null;
}

export function BranchNameField({
  mode,
  value,
  disabled = false,
  refusal = null,
  onChange,
  children,
}: {
  /** Which launch creates the branch; it picks the date fallback's prefix. */
  mode: LaunchMode;
  value: string;
  disabled?: boolean;
  /** A server refusal code from the last start; shown until the name changes. */
  refusal?: string | null;
  onChange: (value: string) => void;
  /** Controls that share the row, such as Code worktree. */
  children?: ReactNode;
}) {
  const warningId = useId();
  // A new refusal pins the value it refused; editing the name clears the warning.
  const [refused, setRefused] = useState({ refusal, value });
  if (refused.refusal !== refusal) setRefused({ refusal, value });
  const trimmed = value.trim();
  const warning =
    branchNameProblem(trimmed) ??
    (refusal && refused.value === value
      ? refusal === "branch_name_taken"
        ? "That name is taken."
        : "The server refused this name."
      : null);
  return (
    <div className="branch-name-field">
      <div className="branch-name-row">
        <label className="branch-name-label">
          <GitBranch size={14} aria-hidden="true" />
          <span>Branch</span>
          <input
            type="text"
            value={value}
            placeholder={branchNamePlaceholder(mode)}
            disabled={disabled}
            spellCheck={false}
            autoCapitalize="off"
            autoComplete="off"
            aria-invalid={warning ? true : undefined}
            aria-describedby={warning ? warningId : undefined}
            onChange={(event) => onChange(event.target.value)}
          />
        </label>
        {children}
      </div>
      {warning && (
        <p id={warningId} className="branch-name-warning" role="alert">
          {warning}
        </p>
      )}
    </div>
  );
}
