// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Whether the configuration a document was processed with switched a step off. The
 * step viewer, its diagram and its details panel each had a copy of this check; they
 * share this one, and the viewer's source defines no other. Policy rule validation is
 * off when it is switched off or the configuration has no policy, for each of its three
 * states; Review Routing is off when its switch is off, and only then.
 */

import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import { isStepDisabled, type StepConfig } from '../is-step-disabled';

const OFF = { enabled: false };
const ON = { enabled: true };

describe('isStepDisabled', () => {
  it.each([[null], [undefined]])('is false without a configuration (%s)', (config) => {
    expect(isStepDisabled('SummarizationStep', config)).toBe(false);
  });

  it.each([
    ['SummarizationStep', { summarization: OFF }, true],
    ['Document Summary', { summarization: OFF }, true],
    ['SummarizationStep', { summarization: ON }, false],
    ['SummarizationStep', {}, false],
    ['EvaluationStep', { evaluation: OFF }, true],
    ['Evaluate Results', { evaluation: OFF }, true],
    ['EvaluationStep', { evaluation: ON }, false],
    ['EvaluationStep', { summarization: OFF }, false],
    ['AssessmentStep', { extraction: { confidence: OFF } }, true],
    ['Assess', { assessment: OFF }, true],
    ['AssessmentStep', { extraction: { confidence: ON } }, false],
    // The v0.6 setting wins over the older one when both are there.
    ['AssessmentStep', { extraction: { confidence: ON }, assessment: OFF }, false],
    ['AssessmentStep', { extraction: { confidence: OFF }, assessment: ON }, true],
    ['AssessmentStep', { extraction: {}, assessment: OFF }, true],
    // Policy rule validation: each of its states, when it is switched off.
    ['PolicyClassificationStep', { rule_validation: OFF, policy_classes: [{}] }, true],
    ['PolicyRuleValidationStep', { rule_validation: OFF, policy_classes: [{}] }, true],
    ['PolicyRuleConsolidationStep', { rule_validation: OFF, policy_classes: [{}] }, true],
    ['PolicyRuleValidationStep', { rule_validation: OFF }, true],
    // ... and when the configuration has no policy to validate against, whatever the switch says.
    ['PolicyRuleValidationStep', { rule_validation: ON, policy_classes: [] }, true],
    ['PolicyClassificationStep', { policy_classes: [] }, true],
    // On with a policy; a configuration that does not say reads as on.
    ['PolicyRuleValidationStep', { rule_validation: ON, policy_classes: [{}] }, false],
    ['PolicyRuleValidationStep', { policy_classes: [{}] }, false],
    ['PolicyRuleValidationStep', { rule_validation: ON }, false],
    ['PolicyRuleValidationStep', {}, false],
    // No other step's switch reaches it, and its switch reaches no other step.
    ['PolicyRuleValidationStep', { evaluation: OFF, summarization: OFF, hitl: OFF }, false],
    ['EvaluationStep', { rule_validation: OFF, policy_classes: [] }, false],
    ['ClassificationStep', { rule_validation: OFF, policy_classes: [] }, false],
    // Review Routing: its switch, and nothing else.
    ['RoutingStep', { hitl: OFF }, true],
    ['Routing Step', { hitl: OFF }, true],
    ['RoutingStep', { hitl: ON }, false],
    ['RoutingStep', {}, false],
    ['RoutingStep', { hitl: {} }, false],
    ['RoutingStep', { rule_validation: OFF, policy_classes: [] }, false],
    // Only the routing state: the switch does not grey out a step that merely has "routing" in its name.
    ['PreRoutingStep', { hitl: OFF }, false],
    ['SummarizationStep', { hitl: OFF }, false],
    // A step no setting switches off.
    ['OCRStep', { summarization: OFF, evaluation: OFF, assessment: OFF, rule_validation: OFF, policy_classes: [], hitl: OFF }, false],
    ['ClassificationStep', { summarization: OFF }, false],
    ['RoutingStep', { evaluation: OFF }, false],
  ] as [string, StepConfig, boolean][])('%s under %j is disabled: %s', (stepName, config, expected) => {
    expect(isStepDisabled(stepName, config)).toBe(expected);
  });

  it('reads the name whatever its case', () => {
    expect(isStepDisabled('SUMMARIZATIONSTEP', { summarization: OFF })).toBe(true);
    expect(isStepDisabled('POLICYRULEVALIDATIONSTEP', { rule_validation: OFF })).toBe(true);
    expect(isStepDisabled('routingstep', { hitl: OFF })).toBe(true);
  });

  it('reads a policy state name however it is spaced', () => {
    expect(isStepDisabled('Policy Rule Validation Step', { policy_classes: [] })).toBe(true);
    expect(isStepDisabled(' PolicyClassificationStep', { rule_validation: OFF })).toBe(true);
  });

  it('is the only definition under the step viewer', () => {
    const folder = join(__dirname, '..');
    const defining = readdirSync(folder)
      .filter((name) => /\.tsx?$/.test(name))
      .filter((name) => /const isStepDisabled\b|function isStepDisabled\b/.test(readFileSync(join(folder, name), 'utf-8')));

    expect(defining).toEqual(['is-step-disabled.ts']);
  });
});
