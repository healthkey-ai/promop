import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'
import noDuplicateTabField from './eslint-rules/no-duplicate-tab-field.js'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
  {
    // No PatientInfo field may render an editable box on two tabs (#955). The
    // rule accumulates keys across the files of one run, so it only holds under
    // a whole-project `eslint .` — which is what `npm run lint` and CI do.
    files: ['src/components/PatientInfo/tabs/**/*.tsx'],
    plugins: { promop: { rules: { 'no-duplicate-tab-field': noDuplicateTabField } } },
    rules: { 'promop/no-duplicate-tab-field': 'error' },
  },
])
