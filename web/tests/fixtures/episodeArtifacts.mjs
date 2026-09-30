export async function mockEpisodeArtifacts(page, entries = []) {
  await page.route("**/api/projects/*/episodes/*/artifacts", (route) =>
    route.fulfill({ json: entries }),
  );
}
