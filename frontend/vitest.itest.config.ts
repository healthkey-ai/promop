import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

/**
 * Round-trip suite: real components, real HTTP, real database.
 *
 * Kept out of the default suite, which must stay hermetic and runnable in CI.
 * This one needs a backend on :8011 against promop_dev:
 *
 *   DATABASE_URL=postgresql://postgres@localhost:5432/promop_dev DEBUG=True \
 *     .venv/bin/python manage.py runserver 8011 --noreload
 *   cd frontend && npx vitest run --config vitest.itest.config.ts
 *
 * The unit tests mock axios, so they prove a component asks for the right thing.
 * They cannot prove the server accepts it, that derivation reads it back, or
 * that the two halves agree on field names and value columns. Every real defect
 * in the writable-UI work was found here rather than there.
 */
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.itest.tsx'],
    testTimeout: 30000,
    // Same origin as the backend, so the session cookie is sent and the '/api'
    // base URL resolves without CORS.
    environmentOptions: { jsdom: { url: 'http://127.0.0.1:8011' } },
  },
});
