import { createRoot } from "react-dom/client";

import { AgentStatusStrip } from "../../src/chat/AgentStatusStrip";
import "../../src/styles.css";

const tasks = [
  {
    operation_id: "turn",
    active: false,
    finished: true,
    native_session_id: null,
    runtime_id: null,
    runtime_label: "Claude stream JSON",
    parent_operation_id: null,
    request: { provider: "claude", model: "opus", session_id: null },
    agent_status: {
      context_tokens: 61_000,
      context_window: 1_000_000,
      autocompact_tokens: 500_000,
      compacting: false,
      compactions: [],
      subagents: [],
    },
  },
];

// A node chat window clips its content and holds the strip at its left; the
// Agents header holds it at its right.
createRoot(document.getElementById("root")!).render(
  <>
    <section
      id="node-window"
      style={{
        position: "absolute",
        left: 40,
        top: 20,
        width: 600,
        height: 500,
        overflow: "hidden",
      }}
    >
      <div style={{ display: "flex", gap: 8, padding: 8 }}>
        <button type="button">Claude · opus · high</button>
        <AgentStatusStrip tasks={tasks as never} />
      </div>
    </section>
    <header
      id="agents-header"
      style={{
        position: "absolute",
        left: 0,
        top: 560,
        width: 1000,
        display: "flex",
        justifyContent: "flex-end",
      }}
    >
      <AgentStatusStrip tasks={tasks as never} />
    </header>
  </>,
);
