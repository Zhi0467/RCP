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
