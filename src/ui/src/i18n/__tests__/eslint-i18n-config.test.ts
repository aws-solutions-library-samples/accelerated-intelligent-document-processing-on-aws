// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The counting rule behind `make check-ui-string-debt`, pinned on fixtures: `eslint.i18n.config.js`
 * run through ESLint's Node API over inline code. What it must count (JSX text, a user-facing
 * attribute, a Cloudscape `i18nStrings` member, a plain `.ts` label), what it must not (technical
 * attributes and tokens, logger and `Error` arguments, a `defineMessages` descriptor), and the one
 * blind spot it has by construction — strings inside a `const ALL_CAPS = ...` declaration — so the
 * known gap declared in `scripts/check_ui_string_debt.py` is measured here rather than assumed.
 */
import { ESLint } from 'eslint';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const UI_ROOT = join(__dirname, '..', '..', '..');
const RULE = 'i18next/no-literal-string';

/** One instance for the file: loading the configuration is the slow part, `lintText` is not. */
const eslint = new ESLint({ cwd: UI_ROOT, overrideConfigFile: 'eslint.i18n.config.js', cache: false, fix: false });

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

  it('names the fix in its message', async () => {
    const [result] = await eslint.lintText("export const labels = { a: 'Hello' };\n", { filePath: 'src/probe/message.ts' });
    expect(result.messages).toHaveLength(1);
    expect(result.messages[0].message).toContain('declare it with defineMessages/FormattedMessage (see src/ui/src/i18n/README.md)');
    expect(result.messages[0].severity).toBe(2);
  });
});
