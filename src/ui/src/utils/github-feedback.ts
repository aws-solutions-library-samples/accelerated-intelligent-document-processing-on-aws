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
 * content into a form.
 *
 * ⚠️ What happens to an id the form does *not* declare is **not documented**.
 * GitHub's docs say an "invalid URL using query parameters" returns a 404, and
 * do not say whether an unknown field id counts as invalid; the commonly
 * observed behaviour is a silent drop. Either way it is bad — a silent drop
 * loses the content, a 404 loses the whole report — so `FORM_FIELDS` is
 * asserted against the YAML in this util's tests rather than trusted.
 *
 * The forms are read from the repository's DEFAULT branch, not from the branch
 * a deployment was built from, so a field added on `develop` is not addressable
 * until it reaches `main`.
 *
 * `title` is sent alongside `template`. GitHub documents `title` with `body`,
 * `labels` and `projects` but never says how it interacts with a form's own
 * `title:` key. Three of the four call sites send exactly the form's default, so
 * the parameter is a no-op for them either way; only the Troubleshoot path
 * differs, and if `title` is ignored there the object key is still carried in
 * the Troubleshoot field.
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
  [BUG_REPORT_TEMPLATE]: ['version', 'region', 'mode', 'troubleshoot', 'additional-context'],
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
 * Map the raw IDPPattern setting to the user-facing processing-mode label the
 * bug form's "Accelerator Processing Mode" field asks for — which is one of
 * `Pipeline mode`, `BDA mode` or both.
 *
 * ⚠️ Returns '' when the setting is not one of those, and `Unified` is the case
 * that matters: every current stack sets `IDPPattern` to exactly that, because
 * the unified pattern can run either branch and which one a document takes is
 * decided per configuration by the `use_bda` flag, which is not knowable here.
 * Echoing `Unified` into that field would answer the form's question with the
 * stack's pattern name, and a field that looks answered stops the triager
 * asking. The raw value is reported as the stack pattern instead, where it is a
 * fact rather than an answer.
 */
const toProcessingMode = (pattern?: string): string => {
  if (!pattern) return '';
  const p = pattern.toLowerCase();
  if (p.includes('bda') || p.includes('pattern1')) return 'BDA mode';
  if (p.includes('pipeline') || p.includes('pattern2')) return 'Pipeline mode';
  return '';
};

/**
 * Human-readable environment block, rendered as a Markdown bullet list.
 *
 * This is what goes into the forms' `version` field, whose own placeholder asks
 * for Version / Build / Stack.
 *
 * `includeDeployment` adds region and processing mode. The bug form has its own
 * fields for those, so it passes `false` to avoid showing a triager the same
 * two values twice; the feature form has neither field, so it passes `true` and
 * they would otherwise be lost from every feature request.
 */
export const buildEnvironmentSummary = (ctx: DeploymentContext, includeDeployment = true): string => {
  const lines: string[] = [];
  if (ctx.version) lines.push(`- **Version:** ${ctx.version}`);
  if (ctx.buildDateTime) lines.push(`- **Build:** ${ctx.buildDateTime}`);
  if (ctx.stackName) lines.push(`- **Stack:** ${ctx.stackName}`);
  const mode = toProcessingMode(ctx.pattern);
  // The raw setting when it is not one of the form's answers — see
  // toProcessingMode. Always reported, because it is the only place `Unified`
  // appears and a triager does need to know it.
  if (ctx.pattern && !mode) lines.push(`- **Stack pattern:** ${ctx.pattern}`);
  if (includeDeployment) {
    if (ctx.region) lines.push(`- **Region:** ${ctx.region}`);
    if (mode) lines.push(`- **Processing mode:** ${mode}`);
  }
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

/**
 * ⚠️ The budget is on the WHOLE URL in bytes, not on characters in one field.
 *
 * GitHub's ceiling is the HTTP request line, measured at 8192 bytes — 8151
 * bytes gets through and 8251 returns `414 URI Too Long`, and the docs confirm
 * the behaviour in words. A character cap cannot stand in for that, because
 * percent-encoding inflates Markdown by roughly 1.8x and far more than that per
 * emoji: `⚠️` is U+26A0 plus U+FE0F, which encodes to `%E2%9A%A0%EF%B8%8F` — 18
 * bytes for two characters. So 6500 characters of agent findings is around
 * 11,000 bytes of query string, and a report that looks capped still 414s and
 * is lost entirely rather than truncated.
 *
 * `URLSearchParams.toString()` emits ASCII, so the assembled URL's `.length` is
 * its byte length and no separate encoder is needed. 7800 leaves room for the
 * request line's method and HTTP-version bytes around the URL.
 */
const MAX_URL_BYTES = 7800;
const TRUNCATION_NOTE = '\n\n…(truncated — use "Copy full details" in the app and paste the rest here)';
// Enough passes for the estimate-driven loop to converge, after which an
// unconditional halving guarantees termination. The estimate is only a way to
// get there in two or three passes instead of twenty; correctness comes from the
// halving and from the final assertion, not from the estimate being right.
const MAX_SHRINK_PASSES = 24;

/**
 * Encoded byte cost of one field, measured with the SAME serializer that
 * assembles the URL.
 *
 * ⚠️ Not `encodeURIComponent`, which disagrees with `URLSearchParams` in both
 * directions: a space is `%20` (3 bytes) for one and `+` (1 byte) for the other,
 * while `!'()~` are literal for one and percent-encoded for the other. Space is
 * by far the commonest of those in Markdown, so `encodeURIComponent` reports
 * about 1.83 bytes/char where the real cost is about 1.14 — a systematic
 * over-estimate, which makes each pass shed only part of the overshoot.
 */
const encodedCost = (id: string, value: string): number => new URLSearchParams([[id, value]]).toString().length;

/**
 * Drop a trailing unpaired high surrogate, so a cut emoji does not leave `�`.
 *
 * Slicing a UTF-16 string can cut between the halves of a surrogate pair — every
 * astral emoji is one, 🔍 and 📄 among them, though `⚠️` is *not*, so an
 * emoji-bearing fixture does not necessarily exercise this.
 *
 * `URLSearchParams` is specified to substitute U+FFFD for an unpaired surrogate,
 * and does: `new URLSearchParams([['k', 'abc\uD83D']]).toString()` is
 * `k=abc%EF%BF%BD`. So this is cosmetic — it keeps a replacement character out
 * of the text a reporter reads — and the bound is worth stating because the
 * cosmetic job is only half done: an interior lone high surrogate, a lone *low*
 * surrogate anywhere, and the second of two consecutive high surrogates all
 * still render as `�`.
 *
 * ⚠️ It is **not** load-bearing for safety, and nothing here is. `encodeURIComponent`
 * *would* throw `URIError` on a lone surrogate, and these builders run in
 * component bodies with no error boundary anywhere in this UI, so that would
 * blank the app — which is why this module assembles every URL through
 * `URLSearchParams` and calls `encodeURIComponent` nowhere. Keep it that way.
 */
const stripLoneSurrogate = (value: string): string => (/[\uD800-\uDBFF]$/.test(value) ? value.slice(0, -1) : value);

/**
 * Cut to at most `keepUnits` UTF-16 units and close an unterminated fence.
 *
 * Deliberately code *units* rather than code points: `String.prototype.slice`
 * is O(keepUnits) where `[...value]` is O(value.length), and these values can be
 * megabytes of agent output while `keepUnits` is a few thousand. Spreading them
 * on every pass of the shrink loop made this function slow enough to block a
 * React render. Surrogate safety comes from the strip above instead.
 *
 * `buildTroubleshootSection` emits a fenced block for the job error and
 * arbitrary Markdown for the findings, so a cut can also land inside a ```
 * fence, after which the truncation note renders as code and everything
 * following it is swallowed.
 */
const shorten = (value: string, keepUnits: number): string => {
  const kept = stripLoneSurrogate(value.slice(0, Math.max(0, keepUnits)));
  const fenceCount = kept.match(/```/g)?.length ?? 0;
  const closed = fenceCount % 2 === 1 ? `${kept}\n\`\`\`` : kept;
  return `${closed}${TRUNCATION_NOTE}`;
};

/**
 * Ceiling applied to every field BEFORE the shrink loop runs.
 *
 * Far more than can fit in the byte budget, so it never changes the output — its
 * job is to bound the work. Without it, a multi-megabyte `findings` string is
 * re-encoded and re-assembled on every pass, which is tens of megabytes of
 * string work inside a component render.
 */
const MAX_FIELD_UNITS = 20000;

/**
 * Assemble a form URL within the byte budget.
 *
 * Empty values are omitted rather than sent blank, so an unfilled field shows
 * the form's own description and placeholder instead of looking like an answered
 * question.
 *
 * When the URL is over budget the **costliest** field is shrunk, repeatedly —
 * costliest in encoded bytes rather than in characters, since a short emoji-rich
 * field can outweigh a longer ASCII one. At most one field per URL carries
 * unbounded content (`troubleshoot` on the bug path, `additional-context` on the
 * feature path); the rest derive from short settings values, so this keeps the
 * small fields intact, which capping a single concatenated body did not.
 *
 * The budget is a guarantee rather than an attempt, and it does not rest on the
 * estimate: a pass whose estimate is unusable halves the field instead, and a
 * terminating fallback below drops fields until the URL fits.
 *
 * ⚠️ Not every pass strictly shrinks its field. `TRUNCATION_NOTE` is appended
 * unconditionally, so for a value under about 149 units halving and re-noting
 * makes it *longer*, converging to a fixed point rather than descending. That is
 * why `MAX_SHRINK_PASSES` and the fallback exist — the floor is around 149 units
 * per field, and five such fields cannot approach the budget.
 */
const buildUrl = (template: string, title: string, fields: Record<string, string | undefined>): string => {
  const entries: [string, string][] = Object.entries(fields)
    .map(([id, value]): [string, string] => [id, value?.trim() ?? ''])
    .filter(([, value]) => value !== '')
    .map(([id, value]): [string, string] => [
      id,
      value.length > MAX_FIELD_UNITS ? shorten(value, MAX_FIELD_UNITS) : stripLoneSurrogate(value),
    ]);

  // The title is clamped rather than shrunk. GitHub caps an issue title at 256
  // characters anyway, and without this a long enough object key could use up
  // the whole budget on its own: the loop below would then strip every field,
  // find nothing left to shrink, and still return an over-budget URL.
  const cappedTitle = stripLoneSurrogate(title.slice(0, 256));

  const assemble = (): string => {
    const usp = new URLSearchParams();
    usp.append('template', template);
    usp.append('title', cappedTitle);
    entries.forEach(([id, value]) => usp.append(id, value));
    return `${GITHUB_NEW_ISSUE_URL}?${usp.toString()}`;
  };

  let url = assemble();
  for (let pass = 0; pass < MAX_SHRINK_PASSES && url.length > MAX_URL_BYTES; pass += 1) {
    let target = -1;
    let targetCost = 0;
    entries.forEach(([id, value], index) => {
      const cost = encodedCost(id, value);
      if (cost > targetCost) {
        target = index;
        targetCost = cost;
      }
    });
    // Only the title and the template remain, and neither is ours to truncate:
    // the title is the one thing a maintainer needs to triage by.
    if (target < 0) break;

    const value = entries[target][1];
    const over = url.length - MAX_URL_BYTES;
    const bytesPerUnit = value.length > 0 ? targetCost / value.length : 1;
    const estimated = value.length - Math.ceil(over / bytesPerUnit) - TRUNCATION_NOTE.length - 8;

    // ⚠️ The halving is the FALLBACK, not a ceiling on the estimate. Capping the
    // estimate at half was measured discarding about half the findings that fit:
    // the estimate subtracts a deliberate safety margin, so the first pass
    // characteristically lands a few dozen bytes over and the second pass then
    // halves a field that was already the right size — 3,783 characters carried
    // where 7,364 fit, in a 4,211-byte URL against a 7,800 budget. Halving when
    // the estimate is unusable is what guarantees progress; halving when it is
    // usable just wastes the budget this loop exists to fill.
    //
    // The same line covers the case where `over` exceeds the field's own length,
    // which happens when a SECOND field is also large (the overshoot is the
    // whole URL's but is charged to one field). Halving instead of cutting to
    // zero means the field is reduced rather than deleted, and the next pass
    // picks whichever field is now costliest — so the shrinking spreads across
    // both instead of destroying one outright with no notice.
    const usable = estimated > 0 && estimated < value.length - TRUNCATION_NOTE.length;
    const keep = usable ? estimated : Math.floor(value.length / 2);
    entries[target][1] = shorten(value, keep);
    url = assemble();

    // A field reduced to just the note cannot shrink further; drop it so the
    // next pass can work on another one.
    if (entries[target][1] === TRUNCATION_NOTE) entries.splice(target, 1);
  }

  // Terminating fallback, so the budget is a guarantee rather than a best
  // effort. Dropping the costliest field outright converges in at most one step
  // per field, and with the title clamped above, template + title alone is a
  // few hundred bytes — so this cannot fail to fit. It should never run: it
  // exists because an estimate-driven loop that merely *usually* converges is
  // how the character cap this replaced came to return URLs GitHub refuses.
  while (url.length > MAX_URL_BYTES && entries.length > 0) {
    let target = 0;
    entries.forEach(([id, value], index) => {
      if (encodedCost(id, value) > encodedCost(entries[target][0], entries[target][1])) target = index;
    });
    entries.splice(target, 1);
    url = assemble();
  }
  return url;
};

/**
 * Bug-report URL. The form supplies the "Describe the bug" prompt, the labels
 * and its own redaction warning, so only the environment and any
 * document/findings context are carried in.
 */
export const buildBugReportUrl = (ctx: DeploymentContext, doc?: DocumentContext, extraContext?: string): string => {
  const title = doc?.objectKey ? `[Bug]: Issue processing ${doc.objectKey}` : '[Bug]: ';

  // ⚠️ Which field the context goes to is decided by WHAT the context is, not by
  // whether a `doc` was passed. The troubleshoot field is labelled "Output of
  // the 'Troubleshoot' agent (if issue is a document processing failure)", so
  // only real agent output belongs there. The Agent Companion Chat also calls
  // this builder, and its answer may have nothing to do with a document
  // failure; filing it under that heading is a wrong label, which is worse than
  // no label — the free-text body this replaced at least made no claim.
  const isAgentOutput = Boolean(doc?.findings || doc?.jobError);
  const section = doc ? buildTroubleshootSection(doc) : '';
  const extra = extraContext?.trim();
  const additional = [isAgentOutput ? '' : section, extra].filter(Boolean).join('\n\n');

  // The redaction reminder rides with whichever field can carry document data;
  // the form's own header carries the general warning for everything else.
  const withNote = (value: string): string => (value ? `${REDACTION_NOTE}\n\n${value}` : '');

  return buildUrl(BUG_REPORT_TEMPLATE, title, {
    version: buildEnvironmentSummary(ctx, false),
    region: ctx.region,
    mode: toProcessingMode(ctx.pattern),
    troubleshoot: withNote(isAgentOutput ? section : ''),
    'additional-context': withNote(additional),
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
