import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

const ALLOWED_COMMENT = /^\s*(eslint-(disable|enable)|@ts-|prettier-ignore)|invariant \d+[a-z]?/i;

/** Ban `//` and non-JSDoc block comments, one report per comment line. */
const noComments = {
  meta: { type: "suggestion", schema: [] },
  create(context) {
    return {
      Program() {
        for (const comment of context.sourceCode.getAllComments()) {
          if (comment.type === "Block" && comment.value.startsWith("*")) continue;
          if (ALLOWED_COMMENT.test(comment.value)) continue;
          const { start, end } = comment.loc;
          for (let line = start.line; line <= end.line; line += 1) {
            context.report({
              loc: { start: { line, column: 0 }, end: { line: line + 1, column: 0 } },
              message: "Write no comments: name it, test it, or put the why in the owning spec.",
            });
          }
        }
      },
    };
  },
};

const rcp = { rules: { "no-comments": noComments } };

/**
 * Only React's two hook rules, plus the comment ban. The hooks plugin's
 * recommended preset also bundles React Compiler rules this app does not adopt.
 */
export default [
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: { parser: tseslint.parser },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    plugins: { "react-hooks": reactHooks, rcp },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
      "rcp/no-comments": "error",
    },
  },
  {
    files: ["tests/**/*.{mjs,ts,tsx}"],
    languageOptions: { parser: tseslint.parser },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    plugins: { rcp },
    rules: { "rcp/no-comments": "error" },
  },
];
