// @ts-check
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// THE COUNTING RULE for the UI string-debt ratchet (issue #1770), NOT a lint gate.
//
// `npm run i18n:lint` runs this configuration alone (`--no-config-lookup`) and
// `scripts/check_ui_string_debt.py` compares its per-file finding counts to the
// committed baseline `scripts/ui_string_debt.json`. It is deliberately NOT part of
// `npm run lint` (which runs with `--max-warnings 0` inside `npm run build`): the
// tree carries thousands of hardcoded user-facing strings today, and the ratchet
// exists so that number can only go down, one converted file at a time.
//
// What counts: every string literal, template literal and JSX text that
// `i18next/no-literal-string` (mode `all`) reaches and that none of the lists
// below sets aside — JSX text, user-facing attributes (`label`, `header`,
// `placeholder`, `ariaLabel`, Cloudscape `i18nStrings` members, ...), object
// properties (column headers, option labels, notification text) and plain `.ts`
// strings (labels, breadcrumbs, notifications). `*.export.ts` modules (CSV and
// markdown text that is English by design) are set aside by a files-scoped
// rule-off at the end of this file.
//
// The lists run in EXCLUDE mode only. Measured on this tree when this configuration was
// written: the plugin skips a scope when ANY ancestor was skipped, so an `include` list
// for attributes, properties or callees hides every descendant of a container it
// does not name (an `i18nStrings` block inside `secondaryContent={...}`, text inside
// a render prop, `steps[].title`, a `.map()` rendering). Therefore each list may hold
// only
//   * LEAF-valued attribute and property names (`className`, `iconName`, `id`, `href`,
//     CSS properties, `data-*`, non-text `aria-*`) — never a container or a callback
//     attribute;
//   * EXPLICIT callee members (`logger.info`, `Object.keys`), never `Object\..*` and
//     never a callback-taking function (`useMemo`, `setTimeout`, `.map`), whose
//     arguments would otherwise vanish from the count;
//   * full-match WORD patterns for technical tokens (punctuation, separators, units,
//     kebab/snake/dotted ids, URLs, MIME types, CSS values, emoji).
// Every string pattern is anchored by the plugin as `^` + pattern + `$`, with no grouping
// of its own, so an alternation has to be grouped here: a bare `a|b` becomes `^a|b$`,
// "starts with a OR ends with b", and excuses every string that merely starts with `a`
// (`eslint-i18n-config.test.ts` fails on a top-level `|`). A RegExp is used as is,
// which is why the emoji pattern is a RegExp with the `u` flag (as a string it would
// lose the flag and miss `✏️`-style sequences with a variation selector).
//
// Known gaps, declared rather than implied away (see the ratchet's docstring):
//   * the plugin skips every string inside a `const ALL_CAPS = ...` declaration
//     unconditionally (`isUpperCase` in its VariableDeclarator visitor; not an option);
//   * `new Error('...')` texts are excluded by design (constructor callees below).
//
// Any edit to this file changes `configSha256` in the baseline and fails the gate
// until `python3 scripts/check_ui_string_debt.py --write` records it, so a widened
// list is always a visible, reviewable diff.
import tseslint from 'typescript-eslint';
import reactPlugin from 'eslint-plugin-react';
import reactHooksPlugin from 'eslint-plugin-react-hooks';
import jsxA11yPlugin from 'eslint-plugin-jsx-a11y';
import importXPlugin from 'eslint-plugin-import-x';
import jestPlugin from 'eslint-plugin-jest';
import prettierPlugin from 'eslint-plugin-prettier';
import i18nextPlugin from 'eslint-plugin-i18next';

/** JSX attributes whose value is never user-facing text. Leaf-valued names only. */
const LEAF_ATTRS = [
  'className',
  'styleName',
  'style',
  'id',
  'key',
  'ref',
  'type',
  'variant',
  'href',
  'src',
  'target',
  'rel',
  'role',
  'width',
  'height',
  'size',
  'color',
  'fontSize',
  'fontWeight',
  'padding',
  'margin',
  'display',
  'float',
  'textAlign',
  'alignment',
  'alignItems',
  'direction',
  'position',
  'iconName',
  'iconAlign',
  'iconUrl',
  'actionButtonIconName',
  'accept',
  'autoComplete',
  'inputMode',
  'spellcheck',
  'tabIndex',
  'name',
  'controlId',
  'descriptionId',
  'labelId',
  'headerSelector',
  'contentType',
  'statusType',
  'colorMode',
  'density',
  'visualMode',
  'language',
  'granularity',
  'xmlns',
  'viewBox',
  'd',
  'fill',
  'stroke',
  'strokeWidth',
  'cx',
  'cy',
  'r',
  'x',
  'y',
  'x1',
  'x2',
  'y1',
  'y2',
  'points',
  'transform',
  'opacity',
  'to',
  'from',
  'index',
  'path',
  'download',
  'media',
  'charset',
  'httpEquiv',
  'data-[a-z-]+',
  'aria-(hidden|live|atomic|busy|controls|describedby|labelledby|owns|expanded|selected|current|haspopup|modal|pressed|checked|disabled|invalid|required|sort|orientation|level|activedescendant|flowto|details|errormessage)',
];

/** CSS properties as object keys (inline `style={{ ... }}` objects and style maps). */
const CSS_KEYS = [
  'fontFamily',
  'fontSize',
  'fontWeight',
  'fontStyle',
  'font',
  'lineHeight',
  'letterSpacing',
  'textAlign',
  'textDecoration',
  'textTransform',
  'textOverflow',
  'textShadow',
  'whiteSpace',
  'wordBreak',
  'overflowWrap',
  'color',
  'background',
  'backgroundColor',
  'backgroundImage',
  'border',
  'borderTop',
  'borderBottom',
  'borderLeft',
  'borderRight',
  'borderColor',
  'borderRadius',
  'borderStyle',
  'borderWidth',
  'boxShadow',
  'boxSizing',
  'outline',
  'display',
  'position',
  'inset',
  'top',
  'left',
  'right',
  'bottom',
  'width',
  'height',
  'minWidth',
  'maxWidth',
  'minHeight',
  'maxHeight',
  'overflow',
  'overflowX',
  'overflowY',
  'flex',
  'flexDirection',
  'flexWrap',
  'flexGrow',
  'flexShrink',
  'flexBasis',
  'justifyContent',
  'alignItems',
  'alignSelf',
  'gap',
  'gridTemplateColumns',
  'gridTemplateRows',
  'gridColumn',
  'gridRow',
  'gridArea',
  'cursor',
  'transition',
  'transform',
  'animation',
  'opacity',
  'zIndex',
  'verticalAlign',
  'listStyle',
  'objectFit',
  'pointerEvents',
  'userSelect',
  'resize',
  'visibility',
  'float',
  'clear',
  'filter',
  'margin[A-Za-z]*',
  'padding[A-Za-z]*',
];

/** Object property names whose value is never user-facing text. Leaf-valued names only. */
const LEAF_KEYS = [
  '[A-Z_-]+',
  'id',
  'key',
  'ref',
  'href',
  'url',
  'uri',
  'src',
  'className',
  'iconName',
  'iconAlign',
  'sortingField',
  'sortingComparator',
  'pattern',
  'regex',
  'testId',
  'fileName',
  'filename',
  'contentType',
  'mimeType',
  'extension',
  'bucket',
  'region',
  'arn',
  'headers',
  'Authorization',
  'Content-Type',
  'Accept',
  'method',
  'encoding',
  'locale',
  'currency',
  'timeZone',
  ...CSS_KEYS,
  '[a-z]+(Id|Key|Path|Url|Uri|Arn|Regex|Pattern)',
];

/**
 * Callees whose string arguments are never user-facing. Explicit members only: the
 * plugin prefixes each entry with `(?:.*\.)?`, so `includes` also matches `x.includes`.
 */
const CALLEES = [
  'logger\\.(debug|info|warn|error|verbose)',
  'console\\.(log|debug|info|warn|error|trace|table|group|groupEnd|assert)',
  'ConsoleLogger',
  'Error',
  'TypeError',
  'RangeError',
  'SyntaxError',
  'ReferenceError',
  'AggregateError',
  '[A-Z][A-Za-z]*Error',
  'DOMException',
  'warn',
  'warnOnce',
  'require',
  'getElementById',
  'querySelector(All)?',
  'getItem',
  'setItem',
  'removeItem',
  'localStorage\\.(getItem|setItem|removeItem|key)',
  'sessionStorage\\.(getItem|setItem|removeItem|key)',
  'includes',
  'indexOf',
  'endsWith',
  'startsWith',
  'split',
  'join',
  'replace',
  'replaceAll',
  'match',
  'test',
  'padStart',
  'padEnd',
  'localeCompare',
  'toLocaleString',
  'toLocaleDateString',
  'toLocaleTimeString',
  'Intl\\.(DateTimeFormat|NumberFormat|RelativeTimeFormat|PluralRules|Collator|ListFormat|DisplayNames|Segmenter)',
  'Date',
  'RegExp',
  'URL',
  'URLSearchParams',
  'Blob',
  'File',
  'encodeURIComponent',
  'decodeURIComponent',
  'JSON\\.(parse|stringify)',
  'Object\\.(keys|values|entries|assign|freeze|fromEntries|defineProperty|create|is|hasOwn|getOwnPropertyNames)',
  'Array\\.(from|isArray|of)',
  'Number',
  'String',
  'parseInt',
  'parseFloat',
  'Symbol',
  'setAttribute',
  'getAttribute',
  'removeAttribute',
  'hasAttribute',
  'classList\\.(add|remove|toggle|contains)',
  'generateClient',
  'client\\.graphql',
  '[A-Za-z_$][\\w$]*\\.graphql',
  'useGraphqlApi',
  'fetchAuthSession',
  'signInWithRedirect',
  'Amplify\\.configure',
  'import',
  'defineMessages',
  'formatMessage',
  'intl\\.(formatMessage|formatDate|formatTime|formatNumber|formatPlural|formatRelativeTime|formatList|formatDisplayName)',
  'i18n(ext)?',
];

/** CSS keyword values, alone or followed by more lowercase tokens (`flex-start center`). */
const CSS_KEYWORDS =
  '(auto|none|inherit|initial|unset|revert|block|inline|inline-block|inline-flex|flex|grid|contents|absolute|relative|fixed|static|sticky|' +
  'hidden|visible|scroll|clip|pointer|default|not-allowed|nowrap|wrap|pre|pre-wrap|pre-line|normal|bold|bolder|lighter|italic|' +
  'center|left|right|top|bottom|middle|baseline|start|end|column|row|column-reverse|row-reverse|solid|dashed|dotted|double|' +
  'transparent|currentColor|monospace|sans-serif|serif|uppercase|lowercase|capitalize|ellipsis|break-word|break-all|cover|contain|' +
  'space-between|space-around|space-evenly|flex-start|flex-end|stretch|underline|line-through|collapse|separate|both|' +
  'ease|ease-in|ease-out|ease-in-out|linear|forwards|infinite)( [a-z-]+)*';

/** A CSS length, alone or in a space-separated list (`8px 16px`). */
const CSS_LENGTH = '\\d+(\\.\\d+)?\\s*(px|em|rem|%|ms|s|vh|vw|fr|pt|deg)';

/**
 * Whole strings that are technical tokens wherever they appear. Each string pattern must
 * match the WHOLE trimmed value, so any alternation in it is grouped (`(?:a|b)`, see the
 * header); the two leading entries are the plugin's own defaults, restated because
 * supplying `words` replaces them.
 */
const WORDS = [
  '[0-9!-/:-@[-`{-~]+',
  '[A-Z_-]+',
  // Emoji (incl. ZWJ sequences and variation selectors), whitespace, punctuation and
  // symbols only. A RegExp so the `u` flag survives; see the header.
  /^[\p{Extended_Pictographic}\p{Emoji_Component}\uFE0F\u200D\s\p{P}\p{S}]+$/u,
  // A single camelCase / lowercase token: variants, statuses, keys (`primary`, `menuDropdown`).
  '[a-z][a-zA-Z0-9]*',
  // kebab/snake/dotted/colon-separated lowercase tokens and their heads (`menu-dropdown`,
  // `idp-locale`, `us-east-1`, `capacity-plan-`, `t:`), model ids, trailing separators.
  '[a-z0-9]*([_.:/-][a-z0-9]*)+',
  // Dotted tokens with any case (`Section.Field`, `file.PDF`, `us.anthropic.claude-3`).
  '[A-Za-z0-9_-]+(\\.[A-Za-z0-9_-]+)+',
  // Paths, anchors and URLs.
  '(\\.{0,2}/|#/|#|https?://|mailto:|data:|s3://|arn:)[^\\s]*',
  // MIME types and slash paths (`image/png`, `docs/guide`).
  '[\\w.+-]+/[\\w.+/-]+',
  // CSS colour functions and custom properties.
  'rgba?\\([^)]*\\)',
  'hsla?\\([^)]*\\)',
  'var\\(--[^)]*\\)',
  // CSS lengths and keyword values.
  `${CSS_LENGTH}(\\s+${CSS_LENGTH})*`,
  CSS_KEYWORDS,
  // Brace placeholders (`{count}`, `${name}`).
  '\\{[^}]*\\}',
  '\\$\\{[^}]*\\}',
  // E-mail addresses.
  '[\\w.-]+@[\\w.-]+',
  // A single letter.
  '[A-Za-z]',
  // Separators and dimension markers.
  '(x|X|\\*|,|;|\\||/|:|–|—|·|•|…|±|→|←|↑|↓|✓|✔|✕|✗|×|°|§|¶|†|‡|~|=|\\+|-)',
  // Units.
  '(px|em|rem|ms|KB|MB|GB|TB|kB|B|KiB|MiB|GiB|TPM|RPM|TPS|RPS|s|min|h|d|%|USD|US\\$)',
  // File formats and HTTP verbs.
  '(json|yaml|yml|csv|pdf|png|jpg|jpeg|tiff|tif|webp|txt|md|zip|xlsx|xls|docx|doc|html|xml|svg)',
  '(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)',
  // MIME type families with parameters (`text/plain;charset=utf-8`).
  'application/.*',
  'text/.*',
  'image/.*',
  // Extension lists (`.pdf,.png`).
  '\\.[a-z0-9]+(,\\s*\\.[a-z0-9]+)*',
  // Literals and DOM/HTML attribute values.
  '(true|false|null|undefined|NaN|Infinity)',
  '(_blank|_self|_parent|_top|noopener noreferrer|noopener,noreferrer|noreferrer|noopener|off|on|numeric|decimal|email|tel|url|search)',
  'Bearer .*',
  '(?:utf-?8|UTF-?8)',
  // Locale tags.
  '(?:en(-[A-Z]{2})?|fr(-[A-Z]{2})?)',
  // PascalCase identifiers and product names (`FormattedMessage`, `GuardDuty`).
  '[A-Z][a-z]+([A-Z][a-z0-9]*)+',
  // `scheme:token` pairs.
  '[a-z]+:[a-z]+',
];

export default tseslint.config(
  {
    ignores: [
      'node_modules/**',
      'build/**',
      'delete/**',
      'tmp/**',
      'src/graphql/generated/**',
      'src/**/*.test.{ts,tsx}',
      'src/**/__tests__/**',
      'src/setupTests.ts',
      'src/**/*.d.ts',
      'src/test-utils/**',
    ],
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    // Every plugin the main configuration registers, rule-less: the tree's existing
    // `eslint-disable` comments name their rules, and an unregistered plugin would turn
    // each of those into a "Definition for rule ... was not found" error that the
    // ratchet would then count as a finding.
    plugins: {
      '@typescript-eslint': tseslint.plugin,
      react: reactPlugin,
      'react-hooks': reactHooksPlugin,
      'jsx-a11y': jsxA11yPlugin,
      'import-x': importXPlugin,
      jest: jestPlugin,
      prettier: prettierPlugin,
      i18next: i18nextPlugin,
    },
    languageOptions: {
      parser: tseslint.parser,
      ecmaVersion: 'latest',
      sourceType: 'module',
      parserOptions: {
        ecmaFeatures: {
          jsx: true,
        },
      },
    },
    linterOptions: {
      // Directive comments written for the main configuration are unused here by design.
      reportUnusedDisableDirectives: 'off',
    },
    rules: {
      'i18next/no-literal-string': [
        'error',
        {
          framework: 'react',
          mode: 'all',
          'should-validate-template': true,
          message: 'hardcoded user-facing string: declare it with defineMessages/FormattedMessage (see src/ui/src/i18n/README.md)',
          'jsx-components': {
            exclude: ['FormattedMessage', 'FormattedPlural', 'code', 'pre', 'kbd', 'style', 'script', 'svg', 'path'],
          },
          'jsx-attributes': { exclude: LEAF_ATTRS },
          'object-properties': { exclude: LEAF_KEYS },
          callees: { exclude: CALLEES },
          words: { exclude: WORDS },
        },
      ],
    },
  },
  // Export modules. CSV column headers and generated report headings are English by
  // design (src/ui/src/i18n/README.md, "The string-debt gate"), so a builder moved into
  // a sibling `<name>.export.ts` module is not counted. The module holds only the export
  // text and the function assembling it; `.ts` cannot hold JSX, so a screen cannot be
  // renamed into the pattern. The pattern matched no existing file when it was added.
  {
    files: ['src/**/*.export.ts'],
    rules: { 'i18next/no-literal-string': 'off' },
  },
);
