// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, it, expect } from 'vitest';

import {
  buildBugReportUrl,
  buildFeatureRequestUrl,
  buildFullDetailsText,
  buildEnvironmentSummary,
  BUG_REPORT_TEMPLATE,
  FEATURE_REQUEST_TEMPLATE,
  FORM_FIELDS,
} from '../github-feedback';

const ctx = {
  version: '0.6.0.dev25',
  region: 'us-west-2',
  stackName: 'IDP1',
  pattern: 'Pattern2 - Packet processing',
  buildDateTime: '2026-07-14T12:00:00Z',
};

/**
 * Read the field ids out of an issue form. Deliberately a regex over the raw
 * YAML rather than a YAML parse: the only thing needed is the `id:` list, and
 * this keeps the UI test tree free of a YAML dependency it has no other use for.
 */
const REPO_ROOT = resolve(__dirname, '../../../../..');

const formFieldIds = (templateFile: string): string[] => {
  const path = resolve(REPO_ROOT, '.github/ISSUE_TEMPLATE', templateFile);
  return [...readFileSync(path, 'utf8').matchAll(/^\s+id:\s*(\S+)\s*$/gm)].map((m) => m[1]);
};

describe('buildEnvironmentSummary', () => {
  it('includes version, build, stack, region, and mode', () => {
    const s = buildEnvironmentSummary(ctx);
    expect(s).toContain('Version:');
    expect(s).toContain('0.6.0.dev25');
    expect(s).toContain('Build:');
    expect(s).toContain('Stack:');
    expect(s).toContain('IDP1');
    expect(s).toContain('Region:');
    expect(s).toContain('us-west-2');
    expect(s).toContain('Processing mode:');
    expect(s).toContain('Pipeline mode');
  });

  it('omits region and mode when the caller has its own fields for them', () => {
    const s = buildEnvironmentSummary(ctx, false);
    expect(s).toContain('0.6.0.dev25');
    expect(s).not.toContain('Region:');
    expect(s).not.toContain('Processing mode:');
  });

  it('omits missing fields', () => {
    expect(buildEnvironmentSummary({ version: '1.0' })).toBe('- **Version:** 1.0');
  });

  it('reports an unrecognised IDPPattern as the stack pattern, not as a processing mode', () => {
    // Every current stack sets IDPPattern to exactly "Unified", which is not one
    // of the three answers the form's Processing Mode field asks for.
    const s = buildEnvironmentSummary({ pattern: 'Unified' });
    expect(s).toContain('Stack pattern:');
    expect(s).toContain('Unified');
    expect(s).not.toContain('Processing mode:');
  });
});

describe('the field ids the forms actually declare', () => {
  // This is the assertion that matters most here. GitHub drops an unknown field
  // id SILENTLY — no error, no warning, the value just never appears — so a
  // typo or a renamed form field would cost the prefill with every test below
  // still passing, because they only read back what this module itself wrote.
  it.each([BUG_REPORT_TEMPLATE, FEATURE_REQUEST_TEMPLATE])('every id written to %s exists in the form', (template) => {
    const declared = formFieldIds(template);
    expect(declared.length).toBeGreaterThan(0);
    for (const id of FORM_FIELDS[template as keyof typeof FORM_FIELDS]) {
      expect(declared).toContain(id);
    }
  });

  it('the bug form still declares the ids the troubleshoot flow depends on', () => {
    expect(formFieldIds(BUG_REPORT_TEMPLATE)).toEqual(
      expect.arrayContaining(['description', 'region', 'mode', 'version', 'troubleshoot', 'additional-context']),
    );
  });

  it('the forms on the DEFAULT branch declare them too, which is the copy GitHub renders', () => {
    // The check above reads the working checkout, which in CI is the PR branch.
    // GitHub renders the form from the repository's default branch, so an id
    // renamed on develop and not yet merged would pass there and break in
    // production. Skips rather than fails where the ref is unavailable (a
    // shallow clone), because a missing ref is not a finding about the code.
    let head: string;
    try {
      head = execFileSync('git', ['symbolic-ref', '--short', 'refs/remotes/github/HEAD'], {
        cwd: REPO_ROOT,
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
      }).trim();
    } catch {
      return; // default-branch ref not fetched in this checkout
    }
    for (const template of [BUG_REPORT_TEMPLATE, FEATURE_REQUEST_TEMPLATE]) {
      const yaml = execFileSync('git', ['show', `${head}:.github/ISSUE_TEMPLATE/${template}`], {
        cwd: REPO_ROOT,
        encoding: 'utf8',
        maxBuffer: 1024 * 1024,
      });
      const declared = [...yaml.matchAll(/^\s+id:\s*(\S+)\s*$/gm)].map((m) => m[1]);
      for (const id of FORM_FIELDS[template as keyof typeof FORM_FIELDS]) {
        expect(declared, `${id} missing from ${template} on ${head}`).toContain(id);
      }
    }
  });

  it('every id the builders actually write is listed in FORM_FIELDS', () => {
    // Closure in the other direction. FORM_FIELDS is what the YAML check reads,
    // so an id the builders write but FORM_FIELDS omits is validated by nothing
    // — which is the silent-drop failure the constant exists to prevent.
    const bugParams = [...new URL(buildBugReportUrl(ctx, { objectKey: 'a.pdf', findings: 'f' }, 'c')).searchParams.keys()];
    const featureParams = [...new URL(buildFeatureRequestUrl(ctx, 'c')).searchParams.keys()];
    const written = (keys: string[]) => keys.filter((k) => k !== 'template' && k !== 'title').sort();

    expect(written(bugParams)).toEqual([...FORM_FIELDS[BUG_REPORT_TEMPLATE]].sort());
    expect(written(featureParams)).toEqual([...FORM_FIELDS[FEATURE_REQUEST_TEMPLATE]].sort());
  });
});

describe('buildBugReportUrl', () => {
  it('selects the bug issue form and never sends body=, which the form would ignore', () => {
    const url = new URL(buildBugReportUrl(ctx));
    expect(url.pathname).toContain('/issues/new');
    expect(url.searchParams.get('template')).toBe(BUG_REPORT_TEMPLATE);
    // `body=` and `template=` are mutually exclusive: sending both means the
    // content is dropped, which is the defect this replaced.
    expect(url.searchParams.get('body')).toBeNull();
    expect(url.searchParams.get('title')).toBe('[Bug]: ');
  });

  it('does not send labels, because the form applies its own', () => {
    // bug_report.yml declares `labels: ["bug"]`. Passing labels= as well is
    // redundant, and on a form submission GitHub takes the form's list.
    const url = new URL(buildBugReportUrl(ctx));
    expect(url.searchParams.get('labels')).toBeNull();
  });

  it('routes the environment, region and mode to their own fields', () => {
    const url = new URL(buildBugReportUrl(ctx));
    expect(url.searchParams.get('region')).toBe('us-west-2');
    expect(url.searchParams.get('mode')).toBe('Pipeline mode');
    const version = url.searchParams.get('version') ?? '';
    expect(version).toContain('0.6.0.dev25');
    expect(version).toContain('IDP1');
  });

  it('maps BDA patterns to "BDA mode"', () => {
    const url = new URL(buildBugReportUrl({ pattern: 'Pattern1 - BDA' }));
    expect(url.searchParams.get('mode')).toBe('BDA mode');
  });

  it('omits a field it has no value for rather than sending it empty', () => {
    // An empty value renders as an answered-but-blank field and hides the
    // form's own placeholder.
    const url = new URL(buildBugReportUrl({ version: '1.0' }));
    expect(url.searchParams.get('region')).toBeNull();
    expect(url.searchParams.get('mode')).toBeNull();
    expect(url.searchParams.get('troubleshoot')).toBeNull();
  });

  it('puts document context and findings in the troubleshoot field, with the redaction reminder', () => {
    const url = new URL(
      buildBugReportUrl(ctx, {
        objectKey: 'lending_package-long.pdf',
        objectStatus: 'FAILED',
        configVersion: '3',
        executionArn: 'arn:aws:states:us-west-2:123:execution:x',
        findings: 'The extraction step timed out.',
      }),
    );
    expect(url.searchParams.get('title')).toContain('lending_package-long.pdf');
    const troubleshoot = url.searchParams.get('troubleshoot') ?? '';
    expect(troubleshoot).toContain('FAILED');
    expect(troubleshoot).toContain('Config profile:');
    expect(troubleshoot).toContain('The extraction step timed out.');
    expect(troubleshoot).toContain('redact');
  });

  it('puts a chat answer in additional-context, not under the Troubleshoot heading', () => {
    // The Agent Companion Chat's answer need not be about a document failure,
    // and the troubleshoot field's label claims it is.
    const url = new URL(buildBugReportUrl(ctx, { objectKey: 'a.pdf' }, 'The agent said the queue was empty.'));
    expect(url.searchParams.get('additional-context')).toContain('The agent said the queue was empty.');
    // The document name is still carried — just not under a heading that claims
    // it is Troubleshoot-agent output.
    expect(url.searchParams.get('additional-context')).toContain('a.pdf');
    expect(url.searchParams.get('troubleshoot')).toBeNull();
  });

  it('keeps real agent output under the Troubleshoot heading', () => {
    const url = new URL(buildBugReportUrl(ctx, { objectKey: 'a.pdf', findings: 'Extraction timed out.' }));
    expect(url.searchParams.get('troubleshoot')).toContain('Extraction timed out.');
    expect(url.searchParams.get('additional-context')).toBeNull();
  });

  it('treats a job error as agent output too', () => {
    const url = new URL(buildBugReportUrl(ctx, { objectKey: 'a.pdf', jobError: 'Lambda timed out' }));
    expect(url.searchParams.get('troubleshoot')).toContain('Lambda timed out');
  });
});

describe('the URL stays inside GitHub’s request-line limit', () => {
  // ⚠️ This is a byte budget, not a character budget, and the distinction is
  // the whole point. GitHub answers 414 above ~8192 bytes of request line, and
  // percent-encoding inflates Markdown ~1.8x and an emoji far more: `⚠️` is
  // U+26A0 + U+FE0F, 18 bytes encoded. A test whose fixture is `'x'.repeat(n)`
  // cannot see this, because `x` does not encode — which is how the previous
  // version of this test certified a URL that returns 414.
  const LIMIT = 8192;

  // Shaped like real agent findings: headings, bullets, a fenced block, emoji.
  const markdownFindings = (lines: number): string =>
    Array.from(
      { length: lines },
      (_, i) =>
        `### ⚠️ Finding ${i + 1}: extraction timed out\n` +
        `- **Step:** \`ExtractionStep\` (shard ${i})\n` +
        '- **Detail:** the agent loop went quiet for 227s, past the socket read timeout\n',
    ).join('\n');

  it.each([
    ['plain ascii', 'x'.repeat(40000)],
    ['markdown with emoji', markdownFindings(400)],
    ['one enormous fenced block', `\`\`\`\n${'é'.repeat(20000)}\n\`\`\``],
  ])('a bug report with %s fits', (_label, findings) => {
    const url = buildBugReportUrl(ctx, { objectKey: 'input/very/long/key/lending_package-long.pdf', jobError: 'boom', findings });
    // URLSearchParams emits ASCII, so .length is the byte length.
    expect(url.length).toBeLessThanOrEqual(LIMIT);
    expect(url).toMatch(/^https:\/\/github\.com\//);
  });

  it('a feature request with a huge chat answer fits', () => {
    const url = buildFeatureRequestUrl(ctx, markdownFindings(400));
    expect(url.length).toBeLessThanOrEqual(LIMIT);
  });

  it('truncation costs the big field and leaves the small ones intact', () => {
    const url = new URL(buildBugReportUrl(ctx, { objectKey: 'a.pdf', findings: markdownFindings(400) }));
    expect(url.searchParams.get('troubleshoot')).toContain('truncated');
    expect(url.searchParams.get('region')).toBe('us-west-2');
    expect(url.searchParams.get('version')).toContain('0.6.0.dev25');
  });

  it('does not truncate when there is no need to', () => {
    const url = new URL(buildBugReportUrl(ctx, { objectKey: 'a.pdf', findings: 'The extraction step timed out.' }));
    const troubleshoot = url.searchParams.get('troubleshoot') ?? '';
    expect(troubleshoot).toContain('The extraction step timed out.');
    expect(troubleshoot).not.toContain('truncated');
  });

  it('closes a code fence the cut landed inside', () => {
    // Everything after an unclosed fence renders as code, so the truncation
    // note — the one line telling the user content is missing — would be
    // swallowed along with it.
    const url = new URL(buildBugReportUrl(ctx, { objectKey: 'a.pdf', jobError: 'boom', findings: `\`\`\`\n${'é'.repeat(20000)}` }));
    const troubleshoot = url.searchParams.get('troubleshoot') ?? '';
    expect(troubleshoot).toContain('truncated');
    expect((troubleshoot.match(/```/g) ?? []).length % 2).toBe(0);
  });
});

describe('buildFeatureRequestUrl', () => {
  it('selects the feature issue form and fills the version field', () => {
    const url = new URL(buildFeatureRequestUrl(ctx));
    expect(url.searchParams.get('template')).toBe(FEATURE_REQUEST_TEMPLATE);
    expect(url.searchParams.get('body')).toBeNull();
    expect(url.searchParams.get('labels')).toBeNull();
    expect(url.searchParams.get('title')).toBe('[Feature]: ');
    const version = url.searchParams.get('version') ?? '';
    expect(version).toContain('0.6.0.dev25');
    expect(version).toContain('us-west-2');
  });

  it('puts provided context (e.g. a chat answer) in additional-context with a redaction reminder', () => {
    // The feature form has no redaction warning of its own, unlike the bug
    // form, so this is the only place that reminder can come from.
    const url = new URL(buildFeatureRequestUrl(ctx, 'It would be great if the agent could export findings.'));
    const additional = url.searchParams.get('additional-context') ?? '';
    expect(additional).toContain('It would be great if the agent could export findings.');
    expect(additional).toContain('redact');
  });

  it('omits additional-context when there is none', () => {
    expect(new URL(buildFeatureRequestUrl(ctx)).searchParams.get('additional-context')).toBeNull();
  });
});

describe('buildFullDetailsText', () => {
  it('includes environment, findings, and the redaction reminder', () => {
    const text = buildFullDetailsText(ctx, { objectKey: 'a.pdf', findings: 'boom' });
    expect(text).toContain('## Environment');
    expect(text).toContain('us-west-2');
    expect(text).toContain('Pipeline mode');
    expect(text).toContain('boom');
    expect(text).toContain('redact');
  });
});
