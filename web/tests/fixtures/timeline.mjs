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

/** The episode resource reads its row and messages beside its timeline. */
export async function mockEpisodeReads(page, episodes = []) {
  await page.route(/\/api\/projects\/[^/]+\/episodes\?episode_id=/, (route) =>
    route.fulfill({ json: episodes }),
  );
  await page.route(/\/api\/projects\/[^/]+\/episodes\/[^/]+\/messages$/, (route) =>
    route.request().method() === "GET" ? route.fulfill({ json: [] }) : route.fallback(),
  );
}
