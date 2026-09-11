// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect } from 'vitest';

import { buildBugReportUrl, buildFeatureRequestUrl, buildFullDetailsText, buildEnvironmentSummary } from '../github-feedback';

const ctx = {
  version: '0.6.0.dev25',
  region: 'us-west-2',
  stackName: 'IDP1',
  pattern: 'Pattern2 - Packet processing',
  buildDateTime: '2026-07-14T12:00:00Z',
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

describe('buildBugReportUrl', () => {
  it('prefills the issue body (not a form template) with a bug label and environment', () => {
    const url = new URL(buildBugReportUrl(ctx));
    expect(url.pathname).toContain('/issues/new');
    expect(url.searchParams.get('template')).toBeNull();
    expect(url.searchParams.get('labels')).toBe('bug');
    const body = url.searchParams.get('body') ?? '';
    expect(body).toContain('## Environment');
    expect(body).toContain('0.6.0.dev25');
    expect(body).toContain('us-west-2');
    expect(body).toContain('Pipeline mode');
    expect(body).toContain('Describe the bug');
    expect(body).toContain('redact'); // redaction reminder
  });

  it('maps BDA patterns to "BDA mode"', () => {
    const url = new URL(buildBugReportUrl({ pattern: 'Pattern1 - BDA' }));
    expect(url.searchParams.get('body')).toContain('BDA mode');
  });

  it('embeds document context and findings in the body when provided', () => {
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
    const body = url.searchParams.get('body') ?? '';
    expect(body).toContain('FAILED');
    expect(body).toContain('Config profile:');
    expect(body).toContain('The extraction step timed out.');
  });

  it('caps an oversized body to keep the URL under GitHub limits', () => {
    const huge = 'x'.repeat(20000);
    const url = new URL(buildBugReportUrl(ctx, { objectKey: 'a.pdf', findings: huge }));
    const body = url.searchParams.get('body') ?? '';
    expect(body.length).toBeLessThan(6600);
    expect(body).toContain('truncated');
  });
});

describe('buildFeatureRequestUrl', () => {
  it('prefills the body with an enhancement label and environment', () => {
    const url = new URL(buildFeatureRequestUrl(ctx));
    expect(url.searchParams.get('template')).toBeNull();
    expect(url.searchParams.get('labels')).toBe('enhancement');
    const body = url.searchParams.get('body') ?? '';
    expect(body).toContain('## Environment');
    expect(body).toContain('0.6.0.dev25');
    expect(body).toContain("Describe the solution you'd like");
  });

  it('embeds provided context (e.g. a chat answer) in the body', () => {
    const url = new URL(buildFeatureRequestUrl(ctx, 'It would be great if the agent could export findings.'));
    const body = url.searchParams.get('body') ?? '';
    expect(body).toContain('## Context');
    expect(body).toContain('It would be great if the agent could export findings.');
    expect(body).toContain('redact');
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
