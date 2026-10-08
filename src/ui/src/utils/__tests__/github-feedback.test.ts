// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
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
const formFieldIds = (templateFile: string): string[] => {
  const path = resolve(__dirname, '../../../../../.github/ISSUE_TEMPLATE', templateFile);
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

  it('omits missing fields', () => {
    expect(buildEnvironmentSummary({ version: '1.0' })).toBe('- **Version:** 1.0');
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
    expect(formFieldIds(BUG_REPORT_TEMPLATE)).toEqual(expect.arrayContaining(['description', 'region', 'mode', 'version', 'troubleshoot']));
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

  it('caps an oversized field to keep the URL under GitHub limits', () => {
    const huge = 'x'.repeat(20000);
    const url = new URL(buildBugReportUrl(ctx, { objectKey: 'a.pdf', findings: huge }));
    const troubleshoot = url.searchParams.get('troubleshoot') ?? '';
    expect(troubleshoot.length).toBeLessThan(6600);
    expect(troubleshoot).toContain('truncated');
    // Capping must not cost the small fields, which is what capping a single
    // concatenated body did.
    expect(url.searchParams.get('region')).toBe('us-west-2');
    expect(url.searchParams.get('version')).toContain('0.6.0.dev25');
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
