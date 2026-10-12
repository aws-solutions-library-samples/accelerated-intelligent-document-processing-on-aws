// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * What the run-level view says when a run was processed with evaluation off.
 *
 * There are two reasons a completed run can have no accuracy metrics, and they
 * are fixed on different screens: the test set has no published ground truth, or
 * the configuration profile has `evaluation.enabled` false. The page had one
 * message and it named the first cause unconditionally, so a run of the second
 * kind sent the reader to the test set to look for missing ground truth that was
 * in fact there. That is the user-visible half of #1330 — the other half being
 * that such a run never reported COMPLETE at all, so this message was unreachable.
 *
 * Same approach as TestResults.draftLabeling: this is JSX gating inside a
 * component whose render needs the GraphQL client, settings and several parsed
 * AWSJSON blobs, so the source is read rather than rendered. The property worth
 * pinning is the mutual exclusion — a second alert that fires alongside the first
 * is as wrong as the wrong message.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'TestResults.tsx'), 'utf-8');

describe('TestResults evaluation-disabled messaging', () => {
  it('names the configuration setting rather than the test set', () => {
    expect(SOURCE).toMatch(/evaluation is turned off in this configuration/);
    expect(SOURCE).toMatch(/evaluationDisabled/);
  });

  it('gates that message on the run having been processed with evaluation off', () => {
    expect(SOURCE).toMatch(/results\.status === 'COMPLETE' && !results\.isDraftLabeling && results\.evaluationDisabled/);
  });

  it('withholds it from a run that does have accuracy data', () => {
    // The conjunct the two gating assertions above do not cover. Dropping it
    // renders this alert alongside the success alert, which the regex for the
    // rest of the condition still matches — measured, so it is asserted
    // separately rather than folded into the patterns above.
    const alerts = SOURCE.match(/\{!hasAccuracyData && results\.status === 'COMPLETE'/g);
    expect(alerts, 'each no-accuracy alert must require !hasAccuracyData').toHaveLength(3);
  });

  it('suppresses the ground-truth message for such a run, so only one alert shows', () => {
    // Without the negation both alerts render, and the one that is wrong about
    // the cause is the one a reader acts on first.
    expect(SOURCE).toMatch(/results\.status === 'COMPLETE' && !results\.isDraftLabeling && !results\.evaluationDisabled/);
  });

  it('keeps the ground-truth message for a run that really has no baseline', () => {
    expect(SOURCE).toMatch(/the test set had no published ground truth/);
  });
});
