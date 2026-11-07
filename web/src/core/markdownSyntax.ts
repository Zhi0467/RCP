import remarkGfm from "remark-gfm";
import remarkMath from "remark-math-extended";

export const ANSWER_SYNTAX_PLUGINS = [remarkGfm, remarkMath] as const;
export const KATEX_OPTIONS = { strict: false, trust: false } as const;
