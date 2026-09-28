import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { test } from "node:test";

import { PHONE_MAX_WIDTH_PX } from "../src/hooks/useNarrowViewport.ts";

const TABLET_MAX_WIDTH_PX = 920;
// Content-driven thresholds: the component each belongs to needs that width.
const CONTENT_THRESHOLDS_PX = new Set([640, 680, 700, 720, 820, 1180]);

function stylesheets() {
  const root = new URL("../src/", import.meta.url);
  const styles = readdirSync(new URL("styles/", root)).map((name) => `styles/${name}`);
  return [...styles, "components/AppearancePicker.css", "components/WorktreeControls.css"].map(
    (path) => readFileSync(new URL(path, root), "utf8"),
  );
}

test("every width query is the phone width, the tablet width, or a known content threshold", () => {
  const widths = stylesheets().flatMap((css) =>
    [...css.matchAll(/@media[^{]*max-width:\s*(\d+)px/g)].map((match) => Number(match[1])),
  );
  assert.ok(widths.includes(PHONE_MAX_WIDTH_PX));
  for (const width of widths) {
    assert.ok(
      width === PHONE_MAX_WIDTH_PX ||
        width === TABLET_MAX_WIDTH_PX ||
        CONTENT_THRESHOLDS_PX.has(width),
      `${width}px`,
    );
  }
});
