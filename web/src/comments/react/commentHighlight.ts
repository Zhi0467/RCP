/** The one highlight registry for quoted comment text, shared by every surface. */
const QUOTED = "rcp-comment";
const SELECTED = "rcp-comment-selected";

const owners = new Map<object, { ranges: readonly Range[]; selected: readonly Range[] }>();

/** Shows `owner`'s quoted ranges in soft red, `selected` stronger, beside other owners' ranges. */
export function showCommentHighlight(
  owner: object,
  ranges: readonly Range[],
  selected: readonly Range[] = [],
) {
  owners.set(owner, { ranges, selected });
  paint();
}

export function clearCommentHighlight(owner: object) {
  if (owners.delete(owner)) paint();
}

function paint() {
  if (typeof CSS === "undefined" || !("highlights" in CSS)) return;
  const quoted: Range[] = [];
  const selected: Range[] = [];
  for (const entry of owners.values()) {
    quoted.push(...entry.ranges);
    selected.push(...entry.selected);
  }
  for (const [name, ranges] of [
    [QUOTED, quoted],
    [SELECTED, selected],
  ] as const) {
    if (ranges.length) CSS.highlights.set(name, new Highlight(...ranges));
    else CSS.highlights.delete(name);
  }
}
