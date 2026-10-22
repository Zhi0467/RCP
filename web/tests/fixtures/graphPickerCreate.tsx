import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { GraphPicker } from "../../src/graph/GraphPicker.tsx";
import { createGraphFork } from "../../src/core/api.ts";
import "../../src/styles.css";

const main = { kind: "main", name: "main", running: false, needs_you: false, archived: false };

function Harness() {
  const [refs, setRefs] = useState([main]);
  const [active, setActive] = useState({ kind: "main" });
  const [revision, setRevision] = useState(7);
  window.advanceHead = () => setRevision((current) => current + 1);
  return React.createElement(GraphPicker, {
    refs,
    activeRef: active,
    projectId: "project",
    onSelect: setActive,
    onRefUpdated() {},
    viewedHead: { target: { kind: "main" }, revision },
    onCreate: async (name, requestId, head) => {
      const ref = await createGraphFork("project", head.target, head.revision, requestId, name);
      setRefs((current) => [...current, ref]);
      setActive({ kind: "branch", branch_id: ref.branch_id });
    },
  });
}

createRoot(document.getElementById("root")).render(React.createElement(Harness));
