import type { Element, ElementContent, Root } from "hast";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema, type Options as SanitizeSchema } from "rehype-sanitize";
import type { PluggableList } from "unified";

const MATH_CLASSES = ["math-inline", "math-display", "language-math"];

/** GitHub's raw-HTML allowlist, keeping the classes math rendering reads. */
export const PAPER_HTML_SCHEMA: SanitizeSchema = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    code: [
      ...(defaultSchema.attributes?.code ?? []),
      ["className", /^language-./, ...MATH_CLASSES],
    ],
    span: [...(defaultSchema.attributes?.span ?? []), ["className", ...MATH_CLASSES]],
    div: [...(defaultSchema.attributes?.div ?? []), ["className", ...MATH_CLASSES]],
  },
};

/** Raw HTML in Paper Markdown, parsed and then reduced to the allowlist. */
export const PAPER_RAW_HTML: PluggableList = [rehypeRaw, [rehypeSanitize, PAPER_HTML_SCHEMA]];

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
    const visit = (children: ElementContent[]) => {
      for (const child of children) {
        if (child.type !== "element") continue;
        if (/^h[1-6]$/.test(child.tagName)) {
          const base = headingSlug(textOf(child));
          const count = seen.get(base) ?? 0;
          seen.set(base, count + 1);
          (child as Element).properties.id = count ? `${base}-${count}` : base;
        } else visit(child.children);
      }
    };
    visit(tree.children as ElementContent[]);
  };
}
