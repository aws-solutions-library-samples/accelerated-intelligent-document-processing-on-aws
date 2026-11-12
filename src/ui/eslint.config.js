// @ts-check
import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';
import reactPlugin from 'eslint-plugin-react';
import reactHooksPlugin from 'eslint-plugin-react-hooks';
import jsxA11yPlugin from 'eslint-plugin-jsx-a11y';
import importXPlugin from 'eslint-plugin-import-x';
import jestPlugin from 'eslint-plugin-jest';
import prettierPlugin from 'eslint-plugin-prettier';
import prettierConfig from 'eslint-config-prettier';
import formatjs from 'eslint-plugin-formatjs';

export default tseslint.config(
  // Global ignores (replaces .eslintignore)
  {
    ignores: [
      'node_modules/**',
      'build/**',
      'delete/**',
      'tmp/**',
      'src/graphql/generated/**',
      'vite.config.js',
    ],
  },

  // Base JS recommended rules
  js.configs.recommended,

  // Base config for all JS/JSX/TS/TSX files
  {
    files: ['src/**/*.{js,jsx,ts,tsx}'],
    plugins: {
      react: reactPlugin,
      'react-hooks': reactHooksPlugin,
      'jsx-a11y': jsxA11yPlugin,
      'import-x': importXPlugin,
      jest: jestPlugin,
      prettier: prettierPlugin,
    },
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: {
        ...globals.browser,
        ...globals.es2021,
        ...globals.jest,
      },
      parserOptions: {
        ecmaFeatures: {
          jsx: true,
        },
      },
    },
    settings: {
      react: {
        version: 'detect',
      },
      'import-x/resolver': {
        node: {
          extensions: ['.js', '.jsx', '.ts', '.tsx'],
        },
      },
    },
    rules: {
      // React recommended rules (manually included since flat config)
      ...reactPlugin.configs.recommended.rules,
      ...jsxA11yPlugin.configs.recommended.rules,

      // Prettier
      'prettier/prettier': [
        'error',
        {
          printWidth: 140,
          singleQuote: true,
          trailingComma: 'all',
        },
      ],

      // General rules
      'no-console': 'off',
      'no-alert': 'off',
      'max-len': [
        'error',
        {
          code: 140,
          ignoreUrls: true,
          ignoreTemplateLiterals: true,
          ignoreComments: true,
          ignoreStrings: true,
          ignoreRegExpLiterals: true,
        },
      ],
      'linebreak-style': ['error', 'unix'],
      'object-curly-newline': ['error', { consistent: true }],

      // React rules
      'react/jsx-filename-extension': ['warn', { extensions: ['.js', '.jsx', '.tsx'] }],
      'react/jsx-wrap-multilines': ['off', { prop: 'parens-new-line' }],
      'react/function-component-definition': ['error', { namedComponents: 'arrow-function' }],
      'react/require-default-props': 'off',
      'react/no-array-index-key': 'warn',
      'react-hooks/exhaustive-deps': 'off',

      // Import rules. `virtual:` is the Vite virtual-module scheme (the i18n catalogs,
      // served by scripts/i18n/vite-plugin-catalogs.mjs), which the node resolver cannot see.
      'import-x/no-unresolved': ['error', { ignore: ['\\.css$', '^virtual:'] }],
      'import-x/extensions': [
        'error',
        'ignorePackages',
        {
          js: 'never',
          jsx: 'never',
          ts: 'never',
          tsx: 'never',
        },
      ],
      'import-x/prefer-default-export': 'off',

      // Misc
      'no-shadow': 'warn',
    },
  },

  // TypeScript-specific overrides
  ...tseslint.configs.recommended.map((config) => ({
    ...config,
    files: ['src/**/*.ts', 'src/**/*.tsx'],
  })),
  {
    files: ['src/**/*.ts', 'src/**/*.tsx'],
    rules: {
      'no-unused-vars': 'off',
      '@typescript-eslint/no-unused-vars': [
        'warn',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          destructuredArrayIgnorePattern: '^_',
          caughtErrors: 'none',
        },
      ],
      'no-shadow': 'off',
      '@typescript-eslint/no-shadow': 'warn',
      '@typescript-eslint/no-explicit-any': 'warn',
      'react/react-in-jsx-scope': 'off',
      'react/prop-types': 'off',
    },
  },

  // Message hygiene for react-intl (issue #1770). Every message is declared with an
  // explicit dotted id, a literal English `defaultMessage` and a literal `description`
  // for the translator; its ICU is valid, its placeholders are passed, and a plural
  // names at least `one` and `other`. Zero findings today; these gate every future
  // message. Tests are out: their <FormattedMessage> fixtures (a dynamic defaultMessage
  // to drive the error path, no translator description) are not messages, and the
  // extraction (`npm run i18n:extract`) ignores the same files. Counting the hardcoded
  // strings that are NOT yet messages is the separate eslint.i18n.config.js, which is
  // deliberately not part of `npm run lint`.
  {
    files: ['src/**/*.ts', 'src/**/*.tsx'],
    ignores: ['src/**/*.test.{ts,tsx}', 'src/**/__tests__/**', 'src/setupTests.ts', 'src/test-utils/**'],
    plugins: {
      formatjs,
    },
    rules: {
      'formatjs/enforce-id': [
        'error',
        {
          idInterpolationPattern: '[sha512:contenthash:base64:6]',
          idWhitelist: ['^[a-z][a-zA-Z0-9]*(\\.[a-zA-Z0-9]+)+$'],
        },
      ],
      'formatjs/enforce-default-message': ['error', 'literal'],
      'formatjs/enforce-description': ['error', 'literal'],
      'formatjs/enforce-placeholders': 'error',
      'formatjs/enforce-plural-rules': ['error', { one: true, other: true }],
      'formatjs/no-invalid-icu': 'error',
      'formatjs/no-multiple-whitespaces': 'error',
      'formatjs/no-offset': 'error',
      'formatjs/prefer-pound-in-plural': 'error',
      'formatjs/no-complex-selectors': ['error', { limit: 20 }],
    },
  },

  // Prettier config last (disables conflicting rules)
  prettierConfig,
);
