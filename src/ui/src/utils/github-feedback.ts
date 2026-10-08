// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Builds GitHub "new issue" URLs for the in-app feedback affordances.
 *
 * Mechanism: these links select one of the issue *forms* in
 * `.github/ISSUE_TEMPLATE/` with `?template=<file>.yml` and pre-fill its
 * individual fields by element id (`?region=us-west-2&mode=...`). The report
 * therefore arrives in the same shape, with the same headings and the same
 * labels, as one filed from GitHub's own "New issue" chooser.
 *
 * ⚠️ `?body=` and `?template=` are MUTUALLY EXCLUSIVE — GitHub ignores `body=`
 * once a template is selected, and conversely a `body=` link bypasses the form
 * and opens the blank editor. So the field ids below are the only way to carry
 * content into a form, and a field id that does not exist in the form is
 * dropped silently: there is no error and no warning, the value simply does not
 * appear. `FORM_FIELDS` is asserted against the YAML in this util's tests for
 * that reason.
 *
 * The forms are read from the repository's DEFAULT branch, not from the branch
 * a deployment was built from, so a field added on `develop` is not addressable
 * until it reaches `main`.
 *
 * Nothing is submitted automatically — GitHub always shows the pre-filled form
 * for the user to review (and redact) before submitting.
 */
import { GITHUB_NEW_ISSUE_URL } from '../constants/github';

/** Issue-form files in `.github/ISSUE_TEMPLATE/`. */
export const BUG_REPORT_TEMPLATE = 'bug_report.yml';
export const FEATURE_REQUEST_TEMPLATE = 'feature_request.yml';

/**
 * The form field ids this util writes to, per template. Exported so the tests
 * can check them against the YAML rather than trusting this list: a typo here
 * costs the field silently.
 */
export const FORM_FIELDS = {
  [BUG_REPORT_TEMPLATE]: ['version', 'region', 'mode', 'troubleshoot'],
  [FEATURE_REQUEST_TEMPLATE]: ['version', 'additional-context'],
} as const;

export interface DeploymentContext {
  /** settings.Version (e.g. "0.6.0.dev25"). */
  version?: string;
  /** VITE_AWS_REGION (e.g. "us-west-2"). */
  region?: string;
  /** settings.StackName. */
  stackName?: string;
  /** settings.IDPPattern — mapped to a friendly processing-mode label. */
  pattern?: string;
  /** settings.BuildDateTime. */
  buildDateTime?: string;
}

/** Optional per-document context, only used by the Troubleshoot flow. */
export interface DocumentContext {
  objectKey?: string;
  objectStatus?: string;
  configVersion?: string;
  executionArn?: string;
  /** Job error message when the troubleshoot job failed. */
  jobError?: string;
  /** Markdown findings text from the Troubleshoot agent result. */
  findings?: string;
}

/**
 * Map the raw IDPPattern setting to the user-facing processing-mode label used
 * in the bug form. IDPPattern values look like "Pattern2 - ..." historically;
 * the unified stack reports BDA vs Pipeline mode.
 */
const toProcessingMode = (pattern?: string): string => {
  if (!pattern) return '';
  const p = pattern.toLowerCase();
  if (p.includes('bda') || p.includes('pattern1')) return 'BDA mode';
  if (p.includes('pipeline') || p.includes('pattern2')) return 'Pipeline mode';
  return pattern;
};

/**
 * Human-readable environment block, rendered as a Markdown bullet list.
 *
 * This is what goes into the forms' `version` field, whose own placeholder asks
 * for Version / Build / Stack. Region and processing mode have dedicated fields
 * on the bug form but not on the feature form, so they stay in this block too —
 * dropping them would lose them from every feature request.
 */
export const buildEnvironmentSummary = (ctx: DeploymentContext): string => {
  const lines: string[] = [];
  if (ctx.version) lines.push(`- **Version:** ${ctx.version}`);
  if (ctx.buildDateTime) lines.push(`- **Build:** ${ctx.buildDateTime}`);
  if (ctx.stackName) lines.push(`- **Stack:** ${ctx.stackName}`);
  if (ctx.region) lines.push(`- **Region:** ${ctx.region}`);
  const mode = toProcessingMode(ctx.pattern);
  if (mode) lines.push(`- **Processing mode:** ${mode}`);
  return lines.join('\n');
};

const REDACTION_NOTE =
  '> ⚠️ Issues on this repository are public. Please review the details below and **redact any sensitive document data** before submitting.';

/** Markdown block describing the document + agent findings for a bug report. */
const buildTroubleshootSection = (doc: DocumentContext): string => {
  const parts: string[] = [];
  const meta: string[] = [];
  if (doc.objectKey) meta.push(`- **Document:** ${doc.objectKey}`);
  if (doc.objectStatus) meta.push(`- **Status:** ${doc.objectStatus}`);
  if (doc.configVersion) meta.push(`- **Config profile:** ${doc.configVersion}`);
  if (doc.executionArn) meta.push(`- **Execution ARN:** ${doc.executionArn}`);
  if (meta.length) parts.push(`## Document context\n${meta.join('\n')}`);
  if (doc.jobError) parts.push(`## Error\n\`\`\`\n${doc.jobError}\n\`\`\``);
  if (doc.findings) parts.push(`## Findings\n${doc.findings}`);
  return parts.join('\n\n');
};

// GitHub rejects/truncates extremely long URLs. The cap is on the ONE field
// that can carry unbounded content (agent findings, a chat answer); the others
// are a version string, a region and a mode. Capping the whole query string
// instead would make which field loses content depend on map ordering.
const MAX_FIELD_CHARS = 6500;
const TRUNCATION_NOTE = '\n\n…(truncated — use "Copy full details" in the app and paste the rest here)';

const capField = (value: string): string =>
  value.length > MAX_FIELD_CHARS ? value.slice(0, MAX_FIELD_CHARS - TRUNCATION_NOTE.length) + TRUNCATION_NOTE : value;

/**
 * Assemble a form URL. Empty values are omitted rather than sent blank, so an
 * unfilled field shows the form's own description and placeholder instead of
 * looking like an answered question.
 */
const buildUrl = (template: string, title: string, fields: Record<string, string | undefined>): string => {
  const usp = new URLSearchParams();
  usp.append('template', template);
  usp.append('title', title);
  Object.entries(fields).forEach(([id, value]) => {
    const trimmed = value?.trim();
    if (trimmed) usp.append(id, capField(trimmed));
  });
  return `${GITHUB_NEW_ISSUE_URL}?${usp.toString()}`;
};

/**
 * Bug-report URL. The form supplies the "Describe the bug" prompt, the labels
 * and its own redaction warning, so only the environment and any
 * document/findings context are carried in.
 */
export const buildBugReportUrl = (ctx: DeploymentContext, doc?: DocumentContext): string => {
  const title = doc?.objectKey ? `[Bug]: Issue processing ${doc.objectKey}` : '[Bug]: ';
  // The troubleshoot field is the one that can carry document data, so the
  // redaction reminder rides with it. The form's header carries the general
  // warning for everything else.
  const troubleshoot = doc ? buildTroubleshootSection(doc) : '';
  return buildUrl(BUG_REPORT_TEMPLATE, title, {
    version: buildEnvironmentSummary(ctx),
    region: ctx.region,
    mode: toProcessingMode(ctx.pattern),
    troubleshoot: troubleshoot ? `${REDACTION_NOTE}\n\n${troubleshoot}` : '',
  });
};

/**
 * Feature-request URL. The form supplies the problem/solution prompts and the
 * `enhancement` label. Unlike the bug form it carries no redaction warning of
 * its own, so one is attached to any app-supplied context (e.g. the chat answer
 * that motivated the request), which is the only part that can hold document
 * data.
 */
export const buildFeatureRequestUrl = (ctx: DeploymentContext, context?: string): string => {
  const trimmed = context?.trim();
  return buildUrl(FEATURE_REQUEST_TEMPLATE, '[Feature]: ', {
    version: buildEnvironmentSummary(ctx),
    'additional-context': trimmed ? `${REDACTION_NOTE}\n\n${trimmed}` : '',
  });
};

/**
 * Plain-text block for the "Copy full details" affordance on the troubleshoot
 * flow — includes everything (environment + document + full findings), since
 * the URL-based prefill is length-capped.
 */
export const buildFullDetailsText = (ctx: DeploymentContext, doc: DocumentContext): string => {
  const sections: string[] = [`## Environment\n${buildEnvironmentSummary(ctx)}`];
  const docBlock = buildTroubleshootSection(doc);
  if (docBlock) sections.push(docBlock);
  sections.push(REDACTION_NOTE);
  return sections.join('\n\n');
};
