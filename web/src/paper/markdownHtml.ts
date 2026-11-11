import type { Element, ElementContent, Root } from "hast";

/** A heading's anchor as GitHub writes it: lowercase, punctuation dropped, spaces as hyphens. */
export function headingSlug(text: string) {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{M}\p{N}\p{Pc} -]/gu, "")
    .replace(/ /g, "-");
}

function textOf(node: ElementContent): string {
  if (node.type === "text") return node.value;
  return node.type === "element" ? node.children.map(textOf).join("") : "";
}

/** Gives each heading its GitHub anchor id, numbering repeats as GitHub does. */
export function rehypeHeadingIds() {
  return (tree: Root) => {
    const seen = new Map<string, number>();
    const used = new Set<string>();
    const visit = (children: ElementContent[]) => {
      for (const child of children) {
        if (child.type !== "element") continue;
        if (/^h[1-6]$/.test(child.tagName)) {
          const base = headingSlug(textOf(child));
          let count = seen.get(base) ?? 0;
          let id = base;
          while (used.has(id)) id = `${base}-${++count}`;
          seen.set(base, count);
          used.add(id);
          (child as Element).properties.id = id;
        } else visit(child.children);
      }
    };
    visit(tree.children as ElementContent[]);
  };
}
