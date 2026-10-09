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

  /**
   * A ref holding the repository's default branch, or null.
   *
   * ⚠️ `actions/checkout` does `git init` + `git fetch` rather than `git clone`,
   * and a remote's HEAD ref is written by clone — so neither CI has it, and a
   * check that silently returns when it is missing passes everywhere while
   * verifying nothing. Candidates are tried in order and the result is reported,
   * so an unread condition is visibly distinct from a satisfied one.
   */
  const defaultBranchRef = (): string | null => {
    const show = (args: string[]): string | null => {
      try {
        return execFileSync('git', args, { cwd: REPO_ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
      } catch {
        return null;
      }
    };
    for (const remote of ['github', 'origin']) {
      const symbolic = show(['symbolic-ref', '--short', `refs/remotes/${remote}/HEAD`]);
      if (symbolic) return symbolic;
    }
    for (const candidate of ['refs/remotes/github/main', 'refs/remotes/origin/main', 'refs/heads/main']) {
      if (show(['rev-parse', '--verify', '--quiet', candidate])) return candidate;
    }
    return null;
  };

  it('the forms on the DEFAULT branch declare them too, which is the copy GitHub renders', (testCtx) => {
    // The check above reads the working checkout, which in CI is the PR branch.
    // GitHub renders the form from the repository's default branch, so an id
    // renamed on develop and not yet merged would pass there and break in
    // production.
    const ref = defaultBranchRef();
    if (!ref) {
      // Reported as a skip, not a pass: this runs in neither CI today, and the
      // working-tree check above is what actually gates there.
      testCtx.skip();
      return;
    }
    for (const template of [BUG_REPORT_TEMPLATE, FEATURE_REQUEST_TEMPLATE]) {
      const yaml = execFileSync('git', ['show', `${ref}:.github/ISSUE_TEMPLATE/${template}`], {
        cwd: REPO_ROOT,
        encoding: 'utf8',
        maxBuffer: 1024 * 1024,
      });
      const declared = [...yaml.matchAll(/^\s+id:\s*(\S+)\s*$/gm)].map((m) => m[1]);
      for (const id of FORM_FIELDS[template as keyof typeof FORM_FIELDS]) {
        expect(declared, `${id} missing from ${template} on ${ref}`).toContain(id);
      }
    }
  });

  // ⚠️ `Pattern2 - …` is a legacy value no deployed stack reports — every
  // current stack sets IDPPattern to exactly `Unified`, under which `mode` is
  // deliberately omitted. So the realistic shape is a SUBSET of FORM_FIELDS, and
  // a strict-equality assertion driven only by the legacy fixture would pass
  // because the fixture is unrealistic. Both are checked: every written id must
  // be listed, and the legacy pattern must reach all of them so the listing is
  // exercised in full rather than only in part.
  it.each([
    ['a unified stack, which is every current deployment', 'Unified'],
    ['a legacy pattern value', 'Pattern2 - Packet processing'],
  ])('every id the builders write for %s is listed in FORM_FIELDS', (_label, pattern) => {
    const withPattern = { ...ctx, pattern };
    const written = (url: string) => [...new URL(url).searchParams.keys()].filter((k) => k !== 'template' && k !== 'title').sort();

    const bug = written(buildBugReportUrl(withPattern, { objectKey: 'a.pdf', findings: 'f' }, 'c'));
    const feature = written(buildFeatureRequestUrl(withPattern, 'c'));

    expect([...FORM_FIELDS[BUG_REPORT_TEMPLATE]].sort()).toEqual(expect.arrayContaining(bug));
    expect([...FORM_FIELDS[FEATURE_REQUEST_TEMPLATE]].sort()).toEqual(expect.arrayContaining(feature));
    if (pattern === 'Pattern2 - Packet processing') {
      // Non-vacuity for the containment above: with mode present, the written
      // set is exactly the listing, so no listed id is unreachable.
      expect(bug).toEqual([...FORM_FIELDS[BUG_REPORT_TEMPLATE]].sort());
      expect(feature).toEqual([...FORM_FIELDS[FEATURE_REQUEST_TEMPLATE]].sort());
    }
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

  // Shaped like real agent findings: headings, bullets, a fenced block, and an
  // ASTRAL emoji. The astral part is load-bearing — 🔍 is a surrogate pair where
  // `⚠️` (U+26A0 + U+FE0F) is two BMP characters, so only the former can be cut
  // in half, and a lone surrogate makes encodeURIComponent throw.
  const markdownFindings = (lines: number): string =>
    Array.from(
      { length: lines },
      (_, i) =>
        `### 🔍 Finding ${i + 1}: extraction timed out ⚠️\n` +
        `- **Step:** \`ExtractionStep\` (shard ${i})\n` +
        '- **Detail:** the agent loop went quiet for 227s, past the socket read timeout\n',
    ).join('\n');

  const FIXTURES: [string, string][] = [
    ['plain ascii', 'x'.repeat(40000)],
    ['markdown with astral emoji', markdownFindings(400)],
    ['one enormous fenced block', `\`\`\`\n${'é'.repeat(20000)}\n\`\`\``],
    // Spaces encode as `+` (1 byte) through URLSearchParams but `%20` (3 bytes)
    // through encodeURIComponent, so a space-dense input is what exposes a
    // budget measured with the wrong serializer. Note it must not be spaces
    // ALONE: `buildUrl` trims each value, so `' '.repeat(n)` is dropped as empty
    // and tests nothing — measured at 548 bytes against the previous commit.
    ['space-dense', `x${' '.repeat(40)}`.repeat(20000)],
    ['nothing but astral emoji', '🔍'.repeat(20000)],
    ['a lone surrogate in the input', `${'a'.repeat(9000)}\uD83D`],
    ['megabytes of indented output', '  indented detail line\n'.repeat(200000)],
  ];

  it.each(FIXTURES)('a bug report with %s fits', (_label, findings) => {
    const url = buildBugReportUrl(ctx, { objectKey: 'input/very/long/key/lending_package-long.pdf', jobError: '```\nboom', findings });
    // URLSearchParams emits ASCII, so .length is the byte length.
    expect(url.length).toBeLessThanOrEqual(LIMIT);
    expect(url).toMatch(/^https:\/\/github\.com\//);
  });

  it.each(FIXTURES)('a feature request with %s fits', (_label, context) => {
    expect(buildFeatureRequestUrl(ctx, context).length).toBeLessThanOrEqual(LIMIT);
  });

  it('never throws, whatever lands in the findings', () => {
    // These builders are called in component bodies and this UI has no error
    // boundary, so a throw here blanks the whole app. A URIError from a cut
    // surrogate pair is the way that happened.
    for (const [, findings] of FIXTURES) {
      for (const key of ['k.pdf', 'z'.repeat(9000), '🔍'.repeat(4000), '\uDC4D']) {
        expect(() => buildBugReportUrl(ctx, { objectKey: key, findings })).not.toThrow();
      }
    }
  });

  it('stays inside the limit at every input size, not just the fixture sizes', () => {
    // The estimate that drives the shrink loop is an estimate; what has to hold
    // is the budget. Stepping the size is how a residual that only appears in a
    // narrow band gets caught — the surrogate crash appeared at 41 of 800 sizes
    // and at none of the three sizes the fixtures used.
    for (const unit of ['x', ' ', '🔍', '### 🔍 F\n- **a:** `b`\n  detail\n']) {
      for (let n = 1; n <= 9000; n += 97) {
        const findings = unit.repeat(Math.ceil(n / unit.length));
        const url = buildBugReportUrl(ctx, { objectKey: 'k.pdf', jobError: '```\nboom', findings });
        expect(url.length, `n=${n} unit=${JSON.stringify(unit)}`).toBeLessThanOrEqual(LIMIT);
      }
    }
  });

  it('a title longer than GitHub accepts cannot consume the whole budget', () => {
    // Without a title clamp a long enough object key starves every field: the
    // loop strips them all, finds nothing left to shrink, and still returns an
    // over-budget URL.
    const url = new URL(buildBugReportUrl(ctx, { objectKey: 'z'.repeat(9000), findings: 'real findings' }));
    expect((url.searchParams.get('title') ?? '').length).toBeLessThanOrEqual(256);
    expect(url.searchParams.get('version')).toContain('0.6.0.dev25');
  });

  it('fills the budget rather than merely staying under it', () => {
    // ⚠️ "Under the limit" is only half the property, and asserting only that
    // half hid a regression that carried 3,783 characters where 7,364 fit — a
    // 4,211-byte URL against a 7,800 budget, because a halving cap was applied
    // to a correct estimate. A truncation that throws away half the findings is
    // not a passing result, so the floor is asserted too.
    for (const findings of ['F'.repeat(30000), markdownFindings(400), '- **Step:** `S` timed out\n'.repeat(400)]) {
      const url = buildBugReportUrl(ctx, { objectKey: 'a.pdf', findings });
      expect(url.length).toBeLessThanOrEqual(LIMIT);
      expect(url.length, `only ${url.length} bytes used of a 7800-byte budget`).toBeGreaterThan(7000);
    }
  });

  it('shrinks both fields rather than deleting one, when both are large', () => {
    // The overshoot belongs to the whole URL but is charged to the field being
    // shrunk, so a second large field used to drive the first one's budget below
    // zero — and it was then removed outright, with no truncation note and no
    // other trace. No call site passes two large fields today; the third
    // parameter exists so one can.
    for (const n of [6000, 8000, 12000, 20000]) {
      const url = new URL(buildBugReportUrl(ctx, { objectKey: 'a.pdf', findings: 'F'.repeat(n) }, 'E'.repeat(n)));
      expect(url.toString().length).toBeLessThanOrEqual(LIMIT);
      expect(url.searchParams.get('troubleshoot'), `troubleshoot deleted at n=${n}`).not.toBeNull();
      expect(url.searchParams.get('additional-context'), `additional-context deleted at n=${n}`).not.toBeNull();
    }
  });

  it('keeps every field present for a realistically large report', () => {
    // The last-resort path drops a field outright with no notice. It must not be
    // reachable by ordinary agent output — only the content inside a field is
    // allowed to be lost, and that loss is marked.
    const url = new URL(buildBugReportUrl(ctx, { objectKey: 'a.pdf', jobError: 'boom', findings: markdownFindings(2000) }, 'chat answer'));
    for (const id of ['version', 'region', 'troubleshoot', 'additional-context']) {
      expect(url.searchParams.get(id), `${id} was dropped`).toBeTruthy();
    }
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
