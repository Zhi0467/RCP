export function timelineFixture(episode_id, mode = "auto_research", overrides = {}) {
  return {
    episode_id,
    mode,
    generated_at: "2026-09-24T12:00:00Z",
    truncated: false,
    members: [],
    actors: [],
    spans: [],
    handoffs: [],
    messages: [],
    signals: [],
    marks: [],
    ...overrides,
  };
}
