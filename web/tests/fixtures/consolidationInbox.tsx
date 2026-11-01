import { createRoot } from "react-dom/client";
import type { ConsolidationInboxItem } from "../../src/core/types";
import { ConsolidationInbox } from "../../src/projects/ConsolidationInbox";
import "../../src/styles.css";

const openedTasks: string[] = [];
Object.assign(window, { openedTasks });
const failure: ConsolidationInboxItem = {
  run_id: "run",
  kind: "failure",
  state: "open",
  graph_target: { kind: "main" },
  graph_branch_name: "main",
  occurrence_date: "2026-10-31",
  created_at: "2026-10-31T00:00:00Z",
  operation_id: "recorded-operation",
  chat_id: null,
  report: null,
  applied_revisions: [],
  revisions_verified: true,
  proposals_created: 0,
  error: { code: "report_missing", message: "Consolidation report was not produced." },
};

createRoot(document.getElementById("root")!).render(
  <ConsolidationInbox
    items={[failure, { ...failure, run_id: "legacy", operation_id: null }]}
    schedule={null}
    needsRenewal={false}
    error={null}
    writesDisabled
    onOpenReport={() => {}}
    onOpenTask={(id) => openedTasks.push(id)}
    onResolve={async () => {}}
    onOpenSettings={() => {}}
  />,
);
