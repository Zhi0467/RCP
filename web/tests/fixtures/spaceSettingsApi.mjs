// Well-formed answers for every read Space Settings makes on open. Anything
// else under /api answers an empty list, so no fixture can reach a backend.
export const MACHINE_POWER_UNSUPPORTED = {
  supported: false,
  installed: false,
  install_problem: null,
  idle_hold: { enabled: false, active: false },
  lid_mode: { enabled: false, active: false },
  demand: false,
  demand_reasons: [],
  latched: null,
  last_release: null,
  cleanup_failure: null,
  external_owner: false,
};

export async function routeSpaceSettingsApi(
  page,
  { machinePower = MACHINE_POWER_UNSUPPORTED } = {},
) {
  await page.route("**/api/**", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/space/machines", (route) => route.fulfill({ json: { machines: [] } }));
  await page.route("**/api/service-connections", (route) =>
    route.fulfill({ json: { dictation: "", connections: [] } }),
  );
  await page.route("**/api/voice/settings", (route) =>
    route.fulfill({
      json: { delegation_model: "", confirm: "tap", live_model: "", idle_minutes: 5 },
    }),
  );
  if (machinePower)
    await page.route("**/api/machine-power", (route) => route.fulfill({ json: machinePower }));
}
