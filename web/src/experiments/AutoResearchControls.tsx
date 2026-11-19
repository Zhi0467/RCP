import type { Ref } from "react";
import { WorktreeRepositoryPicker } from "../chat/WorktreeControls";
import { BranchNameField } from "../graph/BranchNameField";

export function AutoResearchFields({
  budgetInput,
  budget,
  instruction,
  instructionRequired = false,
  branch,
  busy,
  branchNameRefusal = null,
  branchNamesInUse = [],
  setBudget,
  setInstruction,
  setBranch,
}: {
  budgetInput?: Ref<HTMLInputElement>;
  budget: string;
  instruction: string;
  instructionRequired?: boolean;
  branch: string;
  busy: boolean;
  branchNameRefusal?: string | null;
  branchNamesInUse?: readonly string[];
  setBudget: (value: string) => void;
  setInstruction: (value: string) => void;
  setBranch: (value: string) => void;
}) {
  return (
    <>
      <label className="campaign-budget-field">
        <span>Operational invocation ceiling</span>
        <input
          ref={budgetInput}
          type="number"
          min={1}
          step={1}
          inputMode="numeric"
          value={budget}
          disabled={busy}
          onChange={(event) => setBudget(event.target.value)}
        />
      </label>
      <label>
        <span>Starting instruction{instructionRequired ? "" : " (optional)"}</span>
        <textarea
          rows={5}
          required={instructionRequired}
          value={instruction}
          disabled={busy}
          onChange={(event) => setInstruction(event.target.value)}
        />
      </label>
      <div className="campaign-isolation" role="group" aria-label="Isolation">
        <BranchNameField
          mode="auto_research"
          value={branch}
          disabled={busy}
          refusal={branchNameRefusal}
          namesInUse={branchNamesInUse}
          onChange={setBranch}
        />
      </div>
    </>
  );
}

export function AutoResearchWorktree({
  worktreeUnavailable,
  runOn,
  codeWorktreeOn,
  busy,
  setCodeWorktree,
  worktreeRepositories,
  worktreeRepository,
  setWorktreePick,
}: {
  worktreeUnavailable: boolean;
  runOn?: string;
  codeWorktreeOn: boolean;
  busy: boolean;
  setCodeWorktree: (value: boolean) => void;
  worktreeRepositories: string[] | null;
  worktreeRepository: string | null;
  setWorktreePick: (value: string) => void;
}) {
  return (
    <span className="code-worktree-choice">
      <label
        title={
          worktreeUnavailable
            ? `No repository the project's agents can write is on ${runOn}.`
            : undefined
        }
      >
        <input
          type="checkbox"
          checked={codeWorktreeOn}
          disabled={busy || worktreeUnavailable}
          onChange={(event) => setCodeWorktree(event.target.checked)}
        />
        Code worktree
      </label>
      {codeWorktreeOn && worktreeRepositories && (
        <WorktreeRepositoryPicker
          repositories={worktreeRepositories}
          value={worktreeRepository}
          disabled={busy}
          onChange={setWorktreePick}
        />
      )}
    </span>
  );
}
