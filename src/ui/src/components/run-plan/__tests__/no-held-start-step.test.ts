// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The web UI holds no start step of its own. Where a run starts after a review is
 * completed, after a document is sent back and after Process Changes is the server's
 * decision (idp_common.run_plan), and the dialogs show what the server answers. A
 * constant named `..._RESTART_STEP` or `DEFAULT_SEND_BACK_STEP` in a source file would
 * be the UI deciding again, so no source file may hold one. The step catalogue
 * (run-steps.json, generated from the server's step list, and its typed reader
 * run-steps.ts) is where step ids live, and is not scanned.
 */

import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

import { describe, expect, it } from 'vitest';

/** src/ui/src. */
const SRC = join(__dirname, '..', '..', '..');

/** The step catalogue: the generated list and its typed reader. */
const CATALOGUE_FILES = ['components/run-plan/run-steps.json', 'components/run-plan/run-steps.ts'];

const walk = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name);
    return entry.isDirectory() ? walk(full) : [full];
  });

/** A path under src/ui/src, with forward slashes. */
const name = (file: string): string => relative(SRC, file).split(sep).join('/');

/** Every TypeScript source file of the UI: no test, nothing generated, and not the step catalogue. */
const sourceFiles = (): string[] =>
  walk(SRC)
    .map(name)
    .filter((file) => /\.tsx?$/.test(file))
    .filter((file) => !file.includes('__tests__/') && !/\.(test|spec)\.tsx?$/.test(file))
    .filter((file) => !file.startsWith('graphql/generated/'))
    .filter((file) => !CATALOGUE_FILES.includes(file));

/** The source files whose text matches. */
const holders = (pattern: RegExp): string[] => sourceFiles().filter((file) => pattern.test(readFileSync(join(SRC, file), 'utf-8')));

describe('the web UI holds no start step', () => {
  it('scans the UI sources, the two dialogs among them, and not the step catalogue or the tests', () => {
    const files = sourceFiles();

    expect(files).toContain('components/review-queues/ReviewCompletionModal.tsx');
    expect(files).toContain('components/common/ProcessChangesConfirmModal.tsx');
    expect(files).toContain('components/run-plan/PlannedSteps.tsx');
    expect(files).not.toContain('components/run-plan/run-steps.ts');
    expect(files.some((file) => file.includes('__tests__'))).toBe(false);
    // The scan reads files: a pattern every source file holds finds them all.
    expect(holders(/SPDX-License-Identifier|import /).length).toBeGreaterThan(files.length / 2);
  });

  it('has no step a send-back starts at: the server starts it at classification, or at the beginning in BDA mode', () => {
    expect(holders(/DEFAULT_SEND_BACK_STEP/)).toEqual([]);
  });

  it('has no step Process Changes restarts at: the confirmation shows the steps the server returns', () => {
    expect(holders(/_RESTART_STEP\b/)).toEqual([]);
  });
});
