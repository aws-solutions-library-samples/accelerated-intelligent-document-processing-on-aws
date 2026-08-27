// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import type {
  FullConfig,
  FullResult,
  Reporter,
  Suite,
  TestCase,
  TestResult,
} from '@playwright/test/reporter';
import fs from 'node:fs';
import path from 'node:path';

/** Playwright colourises assertion messages; raw escapes corrupt the markdown. */
function stripAnsi(input: string): string {
  // eslint-disable-next-line no-control-regex
  return input.replace(/\u001B\[[0-9;]*m/g, '');
}

interface ScenarioRecord {
  /** Stable per-test id, used to collapse retry attempts into one row. */
  id: string;
  title: string;
  file: string;
  status: TestResult['status'] | 'skipped';
  expected: boolean;
  flaky: boolean;
  retries: number;
  durationMs: number;
  clicks: number;
  fieldEdits: number;
  keystrokes: number;
  navigations: number;
  routes: string[];
  pageErrors: string[];
  consoleErrors: string[];
  error?: string;
}

/**
 * Emits the two artifacts the UAT tier is judged on:
 *
 *   uat-results.json  machine-readable, for trend tracking across runs
 *   uat-report.md     human-readable, for a PR comment / ticket attachment
 *
 * Beyond pass/fail it reports INTERACTION COST per scenario (clicks, keystrokes,
 * navigations, route path). That is a usability signal a normal test report has
 * no opinion about: if "upload a document and see its fields" starts taking 11
 * clicks instead of 6, no assertion fails but the product got worse. Tracking it
 * makes that visible and reviewable.
 */
export default class UatReporter implements Reporter {
  private records: ScenarioRecord[] = [];
  private startedAt = 0;
  private outDir = '.';
  private baseURL = '';

  onBegin(config: FullConfig, _suite: Suite): void {
    this.startedAt = Date.now();
    // Derive from configFile, NOT config.rootDir: rootDir resolves to the
    // project's testDir (./specs), which would bury the report at
    // specs/test-results/ where run_uat.py does not look for it.
    const packageRoot = config.configFile
      ? path.dirname(config.configFile)
      : process.cwd();
    this.outDir = path.join(packageRoot, 'test-results');
    this.baseURL = String(config.projects[0]?.use?.baseURL ?? process.env.UAT_BASE_URL ?? '');
    fs.mkdirSync(this.outDir, { recursive: true });
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    const attachment = result.attachments.find((a) => a.name === 'uat-metrics');
    let metrics = {
      clicks: 0,
      fieldEdits: 0,
      keystrokes: 0,
      navigations: 0,
      routes: [] as string[],
      pageErrors: [] as string[],
      consoleErrors: [] as string[],
    };
    if (attachment?.body) {
      try {
        metrics = { ...metrics, ...JSON.parse(attachment.body.toString('utf-8')) };
      } catch {
        /* keep zeroed metrics rather than failing the report */
      }
    }

    this.records.push({
      id: test.id,
      title: test.title,
      file: path.basename(test.location.file),
      status: result.status,
      expected: result.status === test.expectedStatus,
      // Playwright's definition: eventually passed, but not on the first attempt.
      flaky: result.status === 'passed' && result.retry > 0,
      retries: result.retry,
      durationMs: result.duration,
      ...metrics,
      error: stripAnsi(result.error?.message ?? '')
        .split('\n')
        .slice(0, 4)
        .join(' ')
        .trim() || undefined,
    });
  }

  /**
   * One row per TEST, not per attempt. Playwright calls onTestEnd once per retry,
   * so without this a single flaky/failing scenario is counted 2+ times and the
   * totals overstate both the scenario count and the failure count.
   */
  private finalAttempts(): ScenarioRecord[] {
    const byId = new Map<string, ScenarioRecord>();
    for (const r of this.records) {
      const prior = byId.get(r.id);
      if (!prior || r.retries >= prior.retries) byId.set(r.id, r);
    }
    return [...byId.values()];
  }

  async onEnd(result: FullResult): Promise<void> {
    const totalMs = Date.now() - this.startedAt;
    // Collapse retries before any counting.
    this.records = this.finalAttempts();
    const passed = this.records.filter((r) => r.status === 'passed' && !r.flaky).length;
    const flaky = this.records.filter((r) => r.flaky).length;
    const failed = this.records.filter(
      (r) => r.status === 'failed' || r.status === 'timedOut',
    ).length;
    const skipped = this.records.filter((r) => r.status === 'skipped').length;

    const summary = {
      generatedAt: new Date(this.startedAt + totalMs).toISOString(),
      baseURL: this.baseURL,
      commit: process.env.GITHUB_SHA ?? process.env.UAT_COMMIT ?? null,
      stackName: process.env.UAT_STACK_NAME ?? null,
      overallStatus: result.status,
      durationMs: totalMs,
      counts: { total: this.records.length, passed, failed, flaky, skipped },
      totals: {
        clicks: this.records.reduce((n, r) => n + r.clicks, 0),
        fieldEdits: this.records.reduce((n, r) => n + r.fieldEdits, 0),
        keystrokes: this.records.reduce((n, r) => n + r.keystrokes, 0),
        navigations: this.records.reduce((n, r) => n + r.navigations, 0),
        pageErrors: this.records.reduce((n, r) => n + r.pageErrors.length, 0),
      },
      scenarios: this.records,
    };

    fs.writeFileSync(
      path.join(this.outDir, 'uat-results.json'),
      JSON.stringify(summary, null, 2),
    );
    fs.writeFileSync(path.join(this.outDir, 'uat-report.md'), this.markdown(summary));

    // Surface the summary in the GitHub Actions job page when running in CI.
    if (process.env.GITHUB_STEP_SUMMARY) {
      fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, this.markdown(summary));
    }

    // eslint-disable-next-line no-console
    console.log(
      `\nUAT report: ${path.join(this.outDir, 'uat-report.md')}\n` +
        `UAT results: ${path.join(this.outDir, 'uat-results.json')}`,
    );
  }

  private markdown(s: ReturnType<UatReporter['buildSummaryType']>): string {
    const icon = (r: ScenarioRecord) =>
      r.status === 'passed' ? (r.flaky ? '⚠️ flaky' : '✅ pass') :
      r.status === 'skipped' ? '⏭️ skipped' : '❌ fail';

    const mins = (ms: number) => `${(ms / 1000).toFixed(1)}s`;

    const lines: string[] = [];
    lines.push('# UI Acceptance Test (UAT) Report\n');
    lines.push(`- **Target:** ${s.baseURL || '(unknown)'}`);
    if (s.stackName) lines.push(`- **Stack:** \`${s.stackName}\``);
    if (s.commit) lines.push(`- **Commit:** \`${s.commit}\``);
    lines.push(`- **Generated:** ${s.generatedAt}`);
    lines.push(`- **Duration:** ${mins(s.durationMs)}\n`);

    lines.push(
      `## Result: ${s.counts.failed === 0 ? '✅ all tasks completable' : '❌ ' + s.counts.failed + ' task(s) not completable'}\n`,
    );
    lines.push('| Metric | Value |');
    lines.push('|---|---|');
    lines.push(`| Scenarios | ${s.counts.total} |`);
    lines.push(`| Passed | ${s.counts.passed} |`);
    lines.push(`| Failed | ${s.counts.failed} |`);
    lines.push(`| Flaky (passed on retry) | ${s.counts.flaky} |`);
    lines.push(`| Skipped | ${s.counts.skipped} |`);
    lines.push(`| Uncaught page errors | ${s.totals.pageErrors} |`);
    lines.push('');

    lines.push('## Interaction cost\n');
    lines.push(
      'Clicks, field edits and navigations needed to complete each task. Not assertions — ' +
        'a rising trend means the flow got harder to use even while tests still pass.\n',
    );
    lines.push('| Scenario | Result | Duration | Clicks | Field edits | Navs |');
    lines.push('|---|---|---|---|---|---|');
    for (const r of s.scenarios) {
      lines.push(
        `| ${r.title} | ${icon(r)} | ${mins(r.durationMs)} | ${r.clicks} | ${r.fieldEdits} | ${r.navigations} |`,
      );
    }
    lines.push(
      `| **Total** | | **${mins(s.durationMs)}** | **${s.totals.clicks}** | **${s.totals.fieldEdits}** | **${s.totals.navigations}** |`,
    );
    lines.push('');

    const failures = s.scenarios.filter(
      (r) => r.status === 'failed' || r.status === 'timedOut',
    );
    if (failures.length) {
      lines.push('## Failures\n');
      for (const r of failures) {
        lines.push(`### ❌ ${r.title}`);
        lines.push(`- **Spec:** \`${r.file}\``);
        lines.push(`- **Retries:** ${r.retries}`);
        lines.push(`- **Route path:** ${r.routes.join(' → ') || '(none recorded)'}`);
        if (r.error) lines.push(`- **Error:** ${r.error}`);
        lines.push('');
      }
    }

    const withPageErrors = s.scenarios.filter((r) => r.pageErrors.length);
    if (withPageErrors.length) {
      lines.push('## Uncaught page errors\n');
      lines.push('Any entry here is a defect, even in a scenario that passed.\n');
      for (const r of withPageErrors) {
        lines.push(`- **${r.title}**`);
        for (const e of [...new Set(r.pageErrors)]) lines.push(`  - \`${e}\``);
      }
      lines.push('');
    }

    return lines.join('\n');
  }

  // Type helper only; never called.
  private buildSummaryType() {
    return {
      generatedAt: '',
      baseURL: '',
      commit: null as string | null,
      stackName: null as string | null,
      overallStatus: '' as FullResult['status'],
      durationMs: 0,
      counts: { total: 0, passed: 0, failed: 0, flaky: 0, skipped: 0 },
      totals: { clicks: 0, fieldEdits: 0, keystrokes: 0, navigations: 0, pageErrors: 0 },
      scenarios: [] as ScenarioRecord[],
    };
  }
}
