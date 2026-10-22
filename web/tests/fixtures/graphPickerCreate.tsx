import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { GraphPicker } from "../../src/graph/GraphPicker.tsx";
import { createGraphFork } from "../../src/core/api.ts";
import "../../src/styles.css";

const main = { kind: "main", name: "main", running: false, needs_you: false, archived: false };

function Harness() {
  const [refs, setRefs] = useState([main]);
  const [active, setActive] = useState({ kind: "main" });
  return React.createElement(GraphPicker, {
    refs,
    activeRef: active,
    projectId: "project",
    onSelect: setActive,
    onRefUpdated() {},
    onCreate: async (name, requestId) => {
      const ref = await createGraphFork("project", { kind: "main" }, 7, requestId, name);
      setRefs((current) => [...current, ref]);
      setActive({ kind: "branch", branch_id: ref.branch_id });
    },
  });
}

createRoot(document.getElementById("root")).render(React.createElement(Harness));
