import { readFileSync } from "node:fs";

/** The app stylesheet as the browser sees it: every local import inlined in order. */
export function appStylesheet(entry = new URL("../src/styles.css", import.meta.url)) {
  return readFileSync(entry, "utf8").replace(/^@import "(\.[^"]+)";$/gm, (_, path) =>
    appStylesheet(new URL(path, entry)),
  );
}

/** Replace each `var(--type-*)` with the pixel size the type scale defines. */
export function withResolvedTypeScale(css) {
  const scale = Object.fromEntries(
    [...css.matchAll(/(--type-[0-9a-z]+):\s*([0-9.]+px);/g)].map((match) => [match[1], match[2]]),
  );
  return css.replace(/var\((--type-[0-9a-z]+)\)/g, (token, name) => scale[name] ?? token);
}
