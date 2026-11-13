// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The counting rule behind `make check-ui-string-debt`, pinned on fixtures: `eslint.i18n.config.js`
 * run through ESLint's Node API over inline code. What it must count (JSX text, a user-facing
 * attribute, a Cloudscape `i18nStrings` member, a plain `.ts` label), what it must not (technical
 * attributes and tokens, logger and `Error` arguments, a `defineMessages` descriptor), and the one
 * blind spot it has by construction — strings inside a `const ALL_CAPS = ...` declaration — so the
 * known gap declared in `scripts/check_ui_string_debt.py` is measured here rather than assumed.
 * Also pins the README's example of an English-by-design export module against both configurations:
 * counted on a plain `.ts` path, silent on an `.export.ts` path, clean under `npm run lint`, and the
 * file-local `eslint-disable` comment the README forbids rejected there as an unknown rule.
 */
import { ESLint } from 'eslint';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const UI_ROOT = join(__dirname, '..', '..', '..');
const RULE = 'i18next/no-literal-string';

/** One instance for the file: loading the configuration is the slow part, `lintText` is not. */
const eslint = new ESLint({ cwd: UI_ROOT, overrideConfigFile: 'eslint.i18n.config.js', cache: false, fix: false });

/** The build configuration `npm run lint` runs; it does not know the counting rule. */
const buildLinter = new ESLint({ cwd: UI_ROOT, overrideConfigFile: 'eslint.config.js', cache: false, fix: false });

/**
 * The README's example of an export module, read off the file so the test lints the text a reader
 * copies: the one fence tagged `ts`, at column 0. No dedent logic on purpose: an edit that indents
 * the fence fails here by name instead of being repaired in silence.
 */
const readmeExample = (): string => {
  const readme = readFileSync(join(UI_ROOT, 'src/i18n/README.md'), 'utf8');
  const examples = [...readme.matchAll(/^```ts\n([\s\S]*?)\n```$/gm)].map(([, code]) => code);
  expect(examples, 'the README must carry exactly one `ts` example: that is what this test lints').toHaveLength(1);
  return `${examples[0]}\n`;
};

/** The counted strings in `code`, as `line: text` for a readable diff, under a non-test file name. */
const count = async (code: string, fileName = 'src/probe/Probe.tsx'): Promise<string[]> => {
  const [result] = await eslint.lintText(code, { filePath: fileName });
  const other = result.messages.filter((m) => m.ruleId !== RULE);
  expect(other, 'only the counting rule may report').toEqual([]);
  return result.messages.map((m) => `${m.line}: ${code.split('\n')[m.line - 1].trim()}`);
};

describe('eslint.i18n.config.js, the counting rule of the UI string-debt ratchet', () => {
  it('counts JSX text, a user-facing attribute, an i18nStrings member and a plain .ts label', async () => {
    const tsx = [
      "import { Button, FileUpload } from '@cloudscape-design/components';",
      'export const Panel = () => (',
      '  <div>',
      '    <span>Hello world</span>',
      '    <Button label="Save changes" />',
      "    <FileUpload i18nStrings={{ removeFileAriaLabel: 'Remove file' }} value={[]} onChange={() => undefined} />",
      '  </div>',
      ');',
    ].join('\n');
    expect(await count(tsx)).toEqual([
      '4: <span>Hello world</span>',
      '5: <Button label="Save changes" />',
      "6: <FileUpload i18nStrings={{ removeFileAriaLabel: 'Remove file' }} value={[]} onChange={() => undefined} />",
    ]);

    const ts = "export const navigationLabels = { label: 'Close side navigation' };\n";
    expect(await count(ts, 'src/probe/labels.ts')).toEqual(["1: export const navigationLabels = { label: 'Close side navigation' };"]);
  });

  it('counts nothing technical', async () => {
    const tsx = [
      "import { Box } from '@cloudscape-design/components';",
      "import { defineMessages } from 'react-intl';",
      "import { logger } from '../utils/logger';",
      'export const messages = defineMessages({',
      "  greeting: { id: 'probe.greeting', defaultMessage: 'Hello', description: 'Greeting shown on the probe' },",
      '});',
      'export const open = (url: string) => {',
      "  logger.info('Loading documents');",
      "  if (!url) throw new Error('Failed to parse the configuration');",
      "  window.open(url, '_blank', 'noopener,noreferrer');",
      "  const shade = 'rgba(0,0,0,0.5)';",
      "  document.getElementById('document-list-table')?.setAttribute('data-shade', shade);",
      '};',
      'export const Technical = () => <Box className="probe-panel is-open" variant="primary" data-testid="probe" />;',
    ].join('\n');
    expect(await count(tsx)).toEqual([]);
  });

  it('does NOT count strings inside an ALL-CAPS constant: the declared blind spot', async () => {
    // eslint-plugin-i18next skips every string under `const ALL_CAPS = ...` unconditionally
    // (`isUpperCase` in its VariableDeclarator visitor). The gate's docstring declares this
    // gap and quantifies it; this pins that it is still the plugin's behaviour, so a plugin
    // upgrade that closes it is noticed (the baseline would grow) rather than assumed away.
    expect(await count("export const LABELS = { a: 'Hello' };\n", 'src/probe/caps.ts')).toEqual([]);
    expect(await count("export const labels = { a: 'Hello' };\n", 'src/probe/lower.ts')).toEqual([
      "1: export const labels = { a: 'Hello' };",
    ]);
  });

  it('groups every alternation in its word list, so the plugin’s ^…$ anchoring applies to the whole pattern', async () => {
    // The plugin builds `new RegExp('^' + pattern + '$')` with no grouping of its own, and
    // `|` binds looser than the anchors: a bare `a|b` is "starts with a OR ends with b".
    // Two entries were written that way and excused every string starting with lowercase
    // `en` (`'entitlement push'`) or `utf-8`, and every string ending with `fr`, from the
    // count. Read off the resolved configuration, not a copy of the list.
    const config = await eslint.calculateConfigForFile(join(UI_ROOT, 'src/probe/Probe.tsx'));
    const [, options] = config.rules?.[RULE] as [unknown, { words: { exclude: ReadonlyArray<string | RegExp> } }];
    const topLevelAlternation = (pattern: string): boolean => {
      let depth = 0;
      let inClass = false;
      for (let i = 0; i < pattern.length; i += 1) {
        const c = pattern[i];
        if (c === '\\') i += 1;
        else if (inClass) inClass = c !== ']';
        else if (c === '[') inClass = true;
        else if (c === '(') depth += 1;
        else if (c === ')') depth -= 1;
        else if (c === '|' && depth === 0) return true;
      }
      return false;
    };
    const ungrouped = options.words.exclude.filter((p): p is string => typeof p === 'string' && topLevelAlternation(p));
    expect(ungrouped, 'WORDS entries with a top-level `|`: wrap each as (?:…)').toEqual([]);
    // Guard the guard: the detector sees a bare `|` and ignores a grouped one or one in a class.
    expect(topLevelAlternation('en|fr')).toBe(true);
    expect(topLevelAlternation('(?:en|fr)')).toBe(false);
    expect(topLevelAlternation('[|]')).toBe(false);
    expect(topLevelAlternation('a\\|b')).toBe(false);
  });

  it('excuses a locale tag or a charset as a whole string, but counts a label that merely starts like one', async () => {
    const ts = [
      "export const technical = { lang: 'en', region: 'fr-CA', charset: 'utf-8', upper: 'UTF-8' };",
      "export const label = 'entitlement push';",
      "export const trailing = 'Document is in fr';",
    ].join('\n');
    expect(await count(ts, 'src/probe/tokens.ts')).toEqual([
      "2: export const label = 'entitlement push';",
      "3: export const trailing = 'Document is in fr';",
    ]);
  });

  it('names the fix in its message', async () => {
    const [result] = await eslint.lintText("export const labels = { a: 'Hello' };\n", { filePath: 'src/probe/message.ts' });
    expect(result.messages).toHaveLength(1);
    expect(result.messages[0].message).toContain('declare it with defineMessages/FormattedMessage (see src/ui/src/i18n/README.md)');
    expect(result.messages[0].severity).toBe(2);
  });
});

describe('the README example of an English-by-design export module', () => {
  it('is left alone by the counting configuration on an .export.ts path', async () => {
    const [result] = await eslint.lintText(readmeExample(), { filePath: 'src/components/probe/results.export.ts' });
    expect(result.messages).toEqual([]);
  });

  it('is counted by the counting configuration on a plain .ts path', async () => {
    const [result] = await eslint.lintText(readmeExample(), { filePath: 'src/components/probe/results.ts' });
    expect(result.messages.length, 'the header strings are findings once the file name leaves the pattern').toBeGreaterThan(0);
    const other = result.messages.filter((m) => m.ruleId !== RULE);
    expect(other, 'only the counting rule may report').toEqual([]);
    // Line 2 of the example is the header array. The count itself is not pinned, so a rewording of
    // the example does not fail this test for the wrong reason.
    const lines = result.messages.map((m) => m.line);
    expect(lines, 'the header array is among the findings').toContain(2);
  });

  it('passes the build configuration on the .export.ts path', async () => {
    const [result] = await buildLinter.lintText(readmeExample(), { filePath: 'src/components/probe/results.export.ts' });
    expect(result.messages, 'warnings included: `npm run lint` runs with --max-warnings 0').toEqual([]);
  });

  it('cannot use the file-local eslint-disable comment the README forbids', async () => {
    // The premise of the pattern: the build configuration never registers eslint-plugin-i18next, so
    // ESLint reports the directive as naming an unknown rule. If the build configuration ever
    // registers the plugin, this message changes and the README paragraph must be revisited.
    const code = `/* eslint-disable i18next/no-literal-string */\n${readmeExample()}`;
    const [result] = await buildLinter.lintText(code, { filePath: 'src/components/probe/results.export.ts' });
    expect(result.messages).toHaveLength(1);
    expect(result.messages[0].severity).toBe(2);
    expect(result.messages[0].ruleId).toBe(RULE);
    expect(result.messages[0].message).toContain("Definition for rule 'i18next/no-literal-string' was not found");
  });
});
