// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Builds GitHub "new issue" URLs for the in-app feedback affordances.
 *
 * Mechanism: these app-generated links use the `?title=&body=&labels=` query
 * params, which pre-fill the issue *body* directly with an environment summary
 * plus any provided context (agent findings / chat answer). This works
 * immediately and always carries the content.
 *
 * Note: `?body=` and issue *forms* (`?template=*.yml`) are mutually exclusive —
 * GitHub ignores `body=` when a template is selected. We intentionally use
 * `body=` here so the content is embedded regardless of whether the `.yml`
 * forms exist yet on the repo's default branch. The `.yml` forms still apply
 * when a user clicks "New issue" directly on GitHub.
 *
 * Nothing is submitted automatically — GitHub always shows the pre-filled form
 * for the user to review (and redact) before submitting.
 */
import { GITHUB_NEW_ISSUE_URL } from '../constants/github';

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
 * Human-readable environment block included at the top of every issue body,
 * rendered as a Markdown bullet list.
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

// GitHub rejects/truncates extremely long URLs. Keep the whole URL well under
// the ~8 KB practical ceiling by capping the body.
const MAX_BODY_CHARS = 6500;
const TRUNCATION_NOTE = '\n\n…(truncated — use "Copy full details" in the app and paste the rest here)';

const capBody = (value: string): string =>
  value.length > MAX_BODY_CHARS ? value.slice(0, MAX_BODY_CHARS - TRUNCATION_NOTE.length) + TRUNCATION_NOTE : value;

const buildUrl = (title: string, body: string, labels: string): string => {
  const usp = new URLSearchParams();
  usp.append('title', title);
  usp.append('body', capBody(body));
  if (labels) usp.append('labels', labels);
  return `${GITHUB_NEW_ISSUE_URL}?${usp.toString()}`;
};

/**
 * Bug-report URL. Body carries the environment summary, any document/findings
 * context, and a redaction reminder, plus a "Describe the bug" prompt.
 */
export const buildBugReportUrl = (ctx: DeploymentContext, doc?: DocumentContext): string => {
  const title = doc?.objectKey ? `[Bug]: Issue processing ${doc.objectKey}` : '[Bug]: ';
  const sections = [`## Environment\n${buildEnvironmentSummary(ctx)}`, REDACTION_NOTE, '## Describe the bug\n<!-- What went wrong? -->'];
  if (doc) {
    const ts = buildTroubleshootSection(doc);
    if (ts) sections.push(ts);
  }
  return buildUrl(title, sections.join('\n\n'), 'bug');
};

/**
 * Feature-request URL. Body carries the environment summary and prompts, plus
 * any provided context (e.g. the chat answer that motivated the request).
 */
export const buildFeatureRequestUrl = (ctx: DeploymentContext, context?: string): string => {
  const sections = [
    '## Is your feature request related to a problem?\n<!-- A clear description of the problem. -->',
    "## Describe the solution you'd like\n<!-- What you want to happen. -->",
    `## Environment\n${buildEnvironmentSummary(ctx)}`,
  ];
  if (context && context.trim()) {
    sections.push(`## Context\n${context.trim()}`);
    sections.push(REDACTION_NOTE);
  }
  return buildUrl('[Feature]: ', sections.join('\n\n'), 'enhancement');
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
