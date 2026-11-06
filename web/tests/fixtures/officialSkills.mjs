/** The official catalog: one default-enabled skill and one opt-in workflow. */
export const officialSkills = {
  skill_catalog: [
    {
      id: "graph-audit",
      kind: "skill",
      label: "Graph audit",
      version: "1",
      description: "Check the graph",
      dependencies: [],
    },
    {
      id: "sweep",
      kind: "workflow",
      label: "Sweep",
      version: "1",
      description: "Run a sweep",
      dependencies: [],
    },
  ],
  skill_defaults: { workflow_ids: [], skill_ids: ["graph-audit"] },
};
