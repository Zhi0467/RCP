import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

// Only React's two hook rules. The plugin's recommended preset also bundles
// React Compiler rules this app does not adopt.
export default [
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: { parser: tseslint.parser },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
    },
  },
];
