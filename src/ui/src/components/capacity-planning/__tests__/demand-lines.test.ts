// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The capacity planner's Evaluation and Escalation inputs (#1953).
 *
 * `countLlmComparisons` is checked against every class every shipped preset
 * declares. The counts in `MEASURED_JUDGE_CALLS` were measured, not reasoned: each
 * class was run through the real `EvaluationService.evaluate_document` (Stickler,
 * the IDP LLM comparator and `BedrockClient.invoke_model`, with only the Bedrock
 * runtime client stubbed) on one section whose expected and actual values differ
 * in every field and hold one item in every list, and the stub's `converse` calls
 * were counted. Every class not listed made no call. Two classes could not be
 * measured because the evaluation itself fails on them (an `$id` that is not a
 * valid URI reference), so they are left out rather than recorded as zero.
 */

import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import yaml from 'js-yaml';
import { describe, expect, it } from 'vitest';
import { countLlmComparisons, escalationModelOf, evaluationModelOf } from '../demand-lines';

const REPO_ROOT = join(__dirname, '..', '..', '..', '..', '..', '..');
const PRESETS = join(REPO_ROOT, 'config_library', 'unified');

const MEASURED_JUDGE_CALLS: Array<[string, number, number]> = [
  ['healthcare-multisection-package', 0, 7],
  ['healthcare-multisection-package', 1, 2],
  ['healthcare-multisection-package', 2, 9],
  ['healthcare-multisection-package', 3, 2],
  ['healthcare-multisection-package', 4, 7],
  ['healthcare-multisection-package', 5, 2],
  ['healthcare-multisection-package', 6, 6],
  ['healthcare-multisection-package', 7, 6],
  ['healthcare-multisection-package', 8, 7],
  ['healthcare-multisection-package', 9, 9],
  ['lending-package-sample', 1, 2],
  ['rule-validation', 0, 1],
  ['rule-validation', 1, 5],
  ['rule-validation', 2, 12],
  ['rule-validation', 3, 5],
  ['rule-validation', 4, 1],
];
const UNMEASURABLE: Array<[string, number]> = [
  ['bank-statement-sample', 0],
  ['lending-package-sample-govcloud', 1],
];

function presetClasses(): Array<[string, number, unknown]> {
  const rows: Array<[string, number, unknown]> = [];
  for (const preset of readdirSync(PRESETS).sort()) {
    let text: string;
    try {
      text = readFileSync(join(PRESETS, preset, 'config.yaml'), 'utf-8');
    } catch {
      continue;
    }
    const classes = ((yaml.load(text) as { classes?: unknown[] } | null)?.classes ?? []) as unknown[];
    classes.forEach((klass, index) => rows.push([preset, index, klass]));
  }
  return rows;
}

describe('countLlmComparisons', () => {
  const classes = presetClasses();

  it('reads every preset class, and the measured table names only classes that exist', () => {
    expect(classes.length).toBeGreaterThan(MEASURED_JUDGE_CALLS.length);
    for (const [preset, index] of [...MEASURED_JUDGE_CALLS, ...UNMEASURABLE]) {
      expect(classes.some(([p, i]) => p === preset && i === index)).toBe(true);
    }
  });

  it.each(classes.map(([preset, index, klass]) => ({ preset, index, klass })))(
    '$preset class $index matches the judge calls the evaluation service made',
    ({ preset, index, klass }) => {
      if (UNMEASURABLE.some(([p, i]) => p === preset && i === index)) return;
      const measured = MEASURED_JUDGE_CALLS.find(([p, i]) => p === preset && i === index)?.[2] ?? 0;
      expect(countLlmComparisons(klass)).toBe(measured);
    },
  );

  it('follows the shapes measured on a synthetic class', () => {
    // Measured the same way: 7 calls with one item per list. An object-level LLM
    // method makes none, the LLM leaves of a referenced object count even when the
    // referring property names no method, a list of plain values compared by the
    // judge and an opted-in list-item field each cost one call per pair of items,
    // and a list-item field that did not opt in is downgraded and costs none.
    const synthetic = {
      $id: 'Synth',
      type: 'object',
      $defs: {
        Addr: {
          type: 'object',
          properties: {
            Street: { type: 'string', 'x-aws-idp-evaluation-method': 'LLM' },
            City: { type: 'string', 'x-aws-idp-evaluation-method': 'EXACT' },
          },
        },
        Row: {
          type: 'object',
          properties: {
            Desc: { type: 'string', 'x-aws-idp-evaluation-method': 'LLM' },
            Note: { type: 'string', 'x-aws-idp-evaluation-method': 'LLM', 'x-aws-idp-evaluation-allow-llm-in-list': true },
          },
        },
      },
      properties: {
        Leaf: { type: 'string', 'x-aws-idp-evaluation-method': 'LLM' },
        NumLeaf: { type: 'number', 'x-aws-idp-evaluation-method': 'LLM' },
        BoolLeaf: { type: 'boolean', 'x-aws-idp-evaluation-method': 'LLM' },
        Tags: { type: 'array', items: { type: 'string' }, 'x-aws-idp-evaluation-method': 'LLM' },
        Home: { $ref: '#/$defs/Addr', 'x-aws-idp-evaluation-method': 'LLM' },
        Work: { $ref: '#/$defs/Addr' },
        Rows: { type: 'array', items: { $ref: '#/$defs/Row' } },
        Plain: { type: 'string' },
      },
    };
    expect(countLlmComparisons(synthetic)).toBe(7);
    const quotedOff = structuredClone(synthetic);
    (quotedOff.$defs.Row.properties.Note as Record<string, unknown>)['x-aws-idp-evaluation-allow-llm-in-list'] = 'false';
    expect(countLlmComparisons(quotedOff)).toBe(6);
  });
});

describe('evaluationModelOf', () => {
  it('is the judge model while evaluation is enabled, and nothing otherwise', () => {
    const llm_method = { model: 'us.amazon.nova-2-lite-v1:0' };
    expect(evaluationModelOf({ evaluation: { enabled: true, llm_method } })).toBe('us.amazon.nova-2-lite-v1:0');
    expect(evaluationModelOf({ evaluation: { enabled: false, llm_method } })).toBeUndefined();
    expect(evaluationModelOf({})).toBeUndefined();
  });
});

describe('escalationModelOf', () => {
  const extraction = (validation: Record<string, unknown>) => ({ extraction: { model: 'base-model', validation } });

  it('is the escalation model only while validation failures escalate', () => {
    expect(escalationModelOf(extraction({ enabled: true, fail_action: 'escalate', escalation_model: 'strong' }))).toBe('strong');
    expect(escalationModelOf(extraction({ enabled: true, fail_action: 'warn', escalation_model: 'strong' }))).toBeUndefined();
    expect(escalationModelOf(extraction({ enabled: false, fail_action: 'escalate', escalation_model: 'strong' }))).toBeUndefined();
  });

  it('falls back to the extraction model, which is what escalation then retries on', () => {
    expect(escalationModelOf(extraction({ enabled: true, fail_action: 'escalate', escalation_model: null }))).toBe('base-model');
  });
});
