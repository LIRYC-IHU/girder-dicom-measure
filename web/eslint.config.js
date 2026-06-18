import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';

export default tseslint.config(
  { ignores: ['dist', 'node_modules'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      globals: { ...globals.browser },
    },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
    },
  },
  // Script navigateur injecté dans le client Girder (JS pur, hors src/).
  {
    files: ['public/**/*.js'],
    languageOptions: { globals: { ...globals.browser } },
  },
  // Config Node (Vite/Vitest) — globals Node.
  {
    files: ['*.config.ts', 'vite.config.ts', 'vitest.config.ts'],
    languageOptions: { globals: { ...globals.node } },
  },
);
