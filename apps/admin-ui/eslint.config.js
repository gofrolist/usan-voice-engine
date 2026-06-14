import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

// Flat config (ESLint 9+). Replaces the legacy .eslintrc.cjs + .eslintignore,
// which ESLint 10 no longer supports. Mirrors the previous rule set: it does NOT
// opt into eslint-plugin-react-hooks v7's new React Compiler rules (immutability,
// purity, set-state-in-effect, …) — only the classic rules-of-hooks +
// exhaustive-deps, matching the old `plugin:react-hooks/recommended` behavior.
export default tseslint.config(
  // dist/node_modules: build output. e2e/: Playwright specs that import
  // @playwright/test (installed in CI/P5 only) and live outside tsconfig+vitest.
  { ignores: ["dist", "node_modules", "e2e"] },

  // Base recommended sets (was: extends eslint:recommended +
  // plugin:@typescript-eslint/recommended).
  js.configs.recommended,
  ...tseslint.configs.recommended,

  // Application source.
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: { ...globals.browser },
    },
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },

  // Node-context build/test config files.
  {
    files: ["*.{js,ts}"],
    languageOptions: {
      globals: { ...globals.node },
    },
  },
);
