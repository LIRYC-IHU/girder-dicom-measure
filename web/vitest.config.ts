import { defineConfig } from 'vitest/config';

// Tests unitaires de la logique pure (helpers de mesure, store). Pas de DOM Cornerstone :
// `@cornerstonejs/core` est mocké dans les tests qui en dépendent.
export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
    setupFiles: ['./src/test-setup.ts'],
  },
});
