import { useState } from "react";
import { createRoot } from "react-dom/client";

import type { EvidenceFlag, GraphNode } from "../../src/core/types";
import { EvidenceFlagInbox } from "../../src/projects/EvidenceFlagInbox";
import { evidenceFlagRows } from "../../src/projects/evidenceFlags";
import "../../src/styles.css";

const evidence = { id: "e/claim", type: "evidence", title: "Claim", standing: "asserted" };
const opened: string[] = [];
Object.assign(window, { opened });

function Inbox() {
  const [flag, setFlag] = useState<EvidenceFlag>({
    flag_id: "flag-1",
    run_id: null,
    occurrence_date: "2026-10-17",
    evidence_id: evidence.id,
    evidence_revision: 1,
    kind: "overclaim",
    finding: "The source shows a smaller effect.",
    source: null,
    state: "open",
    chat_id: null,
    created_at: "2026-10-17T00:00:00Z",
  });
  return (
    <EvidenceFlagInbox
      apiBase="/api/projects/demo"
      projectId="demo"
      rows={evidenceFlagRows([flag], { [evidence.id]: evidence as GraphNode }, [])}
      writesDisabled={false}
      onChanged={setFlag}
      onOpenNode={(node) => opened.push(node.id)}
      onOpenChat={(chatId) => opened.push(chatId)}
    />
  );
}

createRoot(document.getElementById("root")!).render(<Inbox />);
