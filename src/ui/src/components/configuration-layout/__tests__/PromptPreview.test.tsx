// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Tests for PromptPreview's model of what actually goes on the wire.
 *
 * `getAttributeNamesForClass` is a deliberate port of
 * `ClassificationService._get_attribute_names_for_class`, and the preview's
 * entire purpose is to show what the model will actually receive. So the cases
 * pinned here are the ones where the two implementations could silently drift —
 * every expectation below is the output the Python test suite asserts for the
 * same schema (see
 * `lib/idp_common_pkg/tests/unit/classification/test_class_and_attribute_names_placeholder.py`).
 *
 * The rest covers the schema-enforcement blocks the preview used to be blind to:
 * the forced toolSpec (Simple + `extraction.forced_tool.enabled`) and the agentic
 * system-prompt restatement (Advanced + `restate_schema_in_system_prompt`, which
 * DEFAULTS ON). Two properties matter most and are asserted through a real render
 * rather than through the helpers alone, because they are what a user reads:
 * whether the block appears at all, and whether the token total moves when the
 * knob does. Before this, turning Schema Enforcement on moved the previewed total
 * by exactly 0.
 */

import React from 'react';
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import PromptPreview, {
  appendClassConfidenceBlock,
  applyModalityToPrompt,
  EXTRACTION_TOOL_NAME,
  expectedSchemaBlockFor,
  extractionModeOf,
  formatToolSpecForPreview,
  getAttributeNamesForClass,
  invalidToolPropertyNames,
  resolveClassificationStepPrompt,
  resolveTopK,
  schemaDivergenceFor,
  simpleIntegratedDowngraded,
  toolInputSchemaFor,
  toolSpecWireText,
} from '../PromptPreview';

describe('getAttributeNamesForClass', () => {
  it('walks flat scalars, nested objects and arrays of objects', () => {
    expect(
      getAttributeNamesForClass({
        properties: {
          borrower: {
            type: 'object',
            properties: {
              name: { type: 'string' },
              address: {
                type: 'object',
                properties: { street: { type: 'string' }, zip: { type: 'string' } },
              },
            },
          },
          loan_amount: { type: 'number' },
          findings: { type: 'array', items: { type: 'string' } },
        },
      }),
    ).toEqual(['borrower.name', 'borrower.address.street', 'borrower.address.zip', 'loan_amount', 'findings']);
  });

  it('surfaces $ref group children exactly like an inline group', () => {
    // The schema editor emits every group as {"$ref": "#/$defs/Name"}, which
    // carries no `type`. Before dereferencing, such a group was emitted as a
    // bare leaf here while the backend walked its children — so the preview
    // showed `Signatures` and the real prompt contained the child names.
    expect(
      getAttributeNamesForClass({
        properties: {
          TaxpayerName: { type: 'string' },
          Signatures: { $ref: '#/$defs/Signatures' },
          InlineGroup: { type: 'object', properties: { Child: { type: 'string' } } },
        },
        $defs: {
          Signatures: {
            type: 'object',
            properties: {
              'Signature-of-taxpayer1': { type: 'boolean' },
              'Signature-of-taxpayer2': { type: 'boolean' },
            },
          },
        },
      }),
    ).toEqual(['TaxpayerName', 'Signatures.Signature-of-taxpayer1', 'Signatures.Signature-of-taxpayer2', 'InlineGroup.Child']);
  });

  it('resolves $ref item shapes, $ref array containers and $ref chains', () => {
    expect(
      getAttributeNamesForClass({
        properties: {
          Transactions: { type: 'array', items: { $ref: '#/$defs/Txn' } },
          Fees: { $ref: '#/$defs/FeeList' },
          Holder: { $ref: '#/$defs/HolderAlias' },
        },
        $defs: {
          Txn: { type: 'object', properties: { date: { type: 'string' }, amount: { type: 'number' } } },
          FeeList: { type: 'array', items: { type: 'object', properties: { label: { type: 'string' } } } },
          HolderAlias: { $ref: '#/$defs/Holder' },
          Holder: { type: 'object', properties: { name: { type: 'string' } } },
        },
      }),
    ).toEqual(['Transactions.date', 'Transactions.amount', 'Fees.label', 'Holder.name']);
  });

  it('terminates on a self-recursive $defs definition', () => {
    expect(
      getAttributeNamesForClass({
        properties: { root: { $ref: '#/$defs/Node' } },
        $defs: {
          Node: {
            type: 'object',
            properties: {
              label: { type: 'string' },
              child: { $ref: '#/$defs/Node' },
              kids: { type: 'array', items: { $ref: '#/$defs/Node' } },
            },
          },
        },
      }),
    ).toEqual(['root.label', 'root.child', 'root.kids']);
  });

  it('degrades on dangling, remote and non-object properties instead of throwing', () => {
    expect(
      getAttributeNamesForClass({
        properties: {
          dangling: { $ref: '#/$defs/Nope' },
          remote: { $ref: 'https://example.com/schema.json' },
          notadict: 'oops' as unknown as Record<string, never>,
          plain: { type: 'string' },
        },
        $defs: {},
      }),
    ).toEqual(['dangling', 'remote', 'plain']);
  });

  it('does not read keys off draft-07 tuple-form items', () => {
    expect(
      getAttributeNamesForClass({
        properties: {
          pair: { type: 'array', items: [{ type: 'string' }, { type: 'number' }] as unknown as Record<string, never> },
          plain: { type: 'string' },
        },
      }),
    ).toEqual(['pair', 'plain']);
  });

  it('bounds a combinatorially expanding $defs DAG', () => {
    // active_refs is per-BRANCH, so a non-cyclic DAG is legitimately re-entered
    // on every sibling branch. Unbounded, a ~2 KB schema expands to hundreds of
    // thousands of names; the result-level soft cap never sees it.
    const depth = 12;
    const fanout = 3;
    const defs: Record<string, unknown> = {};
    for (let i = 0; i < depth; i += 1) {
      const props: Record<string, unknown> = { leaf: { type: 'string' } };
      if (i + 1 < depth) {
        for (let k = 0; k < fanout; k += 1) props[`c${k}`] = { $ref: `#/$defs/L${i + 1}` };
      }
      defs[`L${i}`] = { type: 'object', properties: props };
    }

    const names = getAttributeNamesForClass({
      properties: { root: { $ref: '#/$defs/L0' } },
      $defs: defs,
    });

    expect(names.length).toBe(500); // 10 * MAX_ATTRIBUTES_PER_CLASS
  });

  it('returns an empty list for a class with no properties', () => {
    expect(getAttributeNamesForClass({})).toEqual([]);
  });
});

/**
 * The preview renders the AUTHORED class schema; extraction and assessment send
 * transformed ones. This predicate decides what the "the real prompt differs from
 * this preview" alert claims — so its whole value is being right about which
 * transform applies to which step. Getting that wrong makes the accuracy warning
 * itself inaccurate, which is worse than not having it.
 */
describe('schemaDivergenceFor', () => {
  const on = { extraction: { multi_instance_detection: { enabled: true } } };
  const off = { extraction: { multi_instance_detection: { enabled: false } } };
  const flagged = { 'x-aws-idp-multi-instance': true };

  it('reports neither transform when nothing is enabled', () => {
    expect(schemaDivergenceFor(off, {}, 'extraction')).toEqual({ wrapped: false, probe: false, forcedTool: false, restatesSchema: false });
  });

  it('reports the wrapper on BOTH extraction and confidence', () => {
    // ExtractionService and AssessmentService each wrap in their own
    // _get_class_schema, so the confidence step really does receive the wrapper.
    expect(schemaDivergenceFor(off, flagged, 'extraction').wrapped).toBe(true);
    expect(schemaDivergenceFor(off, flagged, 'confidence').wrapped).toBe(true);
  });

  it('reports the probe on extraction ONLY, because assessment never adds it', () => {
    expect(schemaDivergenceFor(on, {}, 'extraction').probe).toBe(true);
    expect(schemaDivergenceFor(on, {}, 'confidence').probe).toBe(false);
  });

  it('treats the truthy spellings pydantic accepts as enabled', () => {
    // The UI control emits real booleans, but a hand-edited YAML `enabled: yes`
    // coerces to True server-side and would otherwise put the probe on the wire
    // with no warning at all.
    for (const v of [true, 1, 'true', 'True', ' yes ', 'on', '1', 'y']) {
      expect(schemaDivergenceFor({ extraction: { multi_instance_detection: { enabled: v } } }, {}, 'extraction').probe).toBe(true);
    }
  });

  it('treats falsey spellings and absent config as disabled', () => {
    for (const v of [false, 0, 'false', 'no', 'off', '', undefined, null]) {
      expect(schemaDivergenceFor({ extraction: { multi_instance_detection: { enabled: v } } }, {}, 'extraction').probe).toBe(false);
    }
    expect(schemaDivergenceFor(undefined, undefined, 'extraction')).toEqual({
      wrapped: false,
      probe: false,
      forcedTool: false,
      restatesSchema: false,
    });
    expect(schemaDivergenceFor({}, {}, 'extraction').probe).toBe(false);
  });

  it('reports the probe on Simple mode only, because agentic skips it', () => {
    // _build_wire_schema returns the schema untouched when agentic.enabled, so
    // claiming the probe in Advanced mode warned about something never sent.
    expect(
      schemaDivergenceFor({ extraction: { mode: 'simple', multi_instance_detection: { enabled: true } } }, {}, 'extraction').probe,
    ).toBe(true);
    expect(
      schemaDivergenceFor({ extraction: { mode: 'advanced', multi_instance_detection: { enabled: true } } }, {}, 'extraction').probe,
    ).toBe(false);
  });

  describe('forcedTool', () => {
    const forcing = (extra: Record<string, unknown> = {}) => ({ extraction: { forced_tool: { enabled: true }, ...extra } });

    it('is on for Simple + Schema Enforcement, and off when the knob is off', () => {
      expect(schemaDivergenceFor(forcing({ mode: 'simple' }), {}, 'extraction').forcedTool).toBe(true);
      expect(schemaDivergenceFor({ extraction: { mode: 'simple', forced_tool: { enabled: false } } }, {}, 'extraction').forcedTool).toBe(
        false,
      );
      // Absent key defaults FALSE — ForcedToolConfig.enabled.
      expect(schemaDivergenceFor({ extraction: { mode: 'simple' } }, {}, 'extraction').forcedTool).toBe(false);
    });

    it('is off in Advanced mode even with the knob on, because only Simple sends it', () => {
      expect(schemaDivergenceFor(forcing({ mode: 'advanced' }), {}, 'extraction').forcedTool).toBe(false);
      // mode is authoritative over agentic.enabled (reconcile_mode_and_agentic),
      // so a stale agentic.enabled must not resurrect the tab.
      expect(schemaDivergenceFor(forcing({ mode: 'simple', agentic: { enabled: true } }), {}, 'extraction').forcedTool).toBe(true);
      // ...and a legacy config with no mode has it inferred from agentic.enabled.
      expect(schemaDivergenceFor(forcing({ agentic: { enabled: true } }), {}, 'extraction').forcedTool).toBe(false);
      expect(schemaDivergenceFor(forcing(), {}, 'extraction').forcedTool).toBe(true);
    });

    it('never leaks into the confidence step, which sends no forced tool', () => {
      expect(schemaDivergenceFor(forcing({ mode: 'simple' }), {}, 'confidence').forcedTool).toBe(false);
    });

    it('is off on the two models that reject a forced toolChoice, and on in the same config for their 5.0 siblings', () => {
      // Mirrors FORCED_TOOL_CHOICE_UNSUPPORTED in idp_common/bedrock/client.py.
      // Both halves of each pair matter: the ids are substrings of one another
      // ('claude-sonnet-5' inside 'claude-sonnet-5-5'), so a check written against
      // the shorter id would turn the tab off for Sonnet 5 as well — which DOES
      // accept forcing — and a check that is simply missing turns it on for 5.5,
      // showing a Tool Schema tab for a request Bedrock answers with a 400 and
      // folding the toolSpec's tokens into the displayed input-token estimate.
      const simple = (model: string) => forcing({ mode: 'simple', model });

      expect(schemaDivergenceFor(simple('us.anthropic.claude-sonnet-5-5'), {}, 'extraction').forcedTool).toBe(false);
      expect(schemaDivergenceFor(simple('global.anthropic.claude-sonnet-5-5'), {}, 'extraction').forcedTool).toBe(false);
      expect(schemaDivergenceFor(simple('us.anthropic.claude-opus-5-5'), {}, 'extraction').forcedTool).toBe(false);

      expect(schemaDivergenceFor(simple('us.anthropic.claude-sonnet-5'), {}, 'extraction').forcedTool).toBe(true);
      expect(schemaDivergenceFor(simple('us.anthropic.claude-sonnet-5:1m'), {}, 'extraction').forcedTool).toBe(true);
      expect(schemaDivergenceFor(simple('us.anthropic.claude-opus-5'), {}, 'extraction').forcedTool).toBe(true);
      // Haiku 5.5 shares both 5.5 models' sampling and effort surface but accepts
      // forcing, so "another 5.5" must not become the rule here either.
      expect(schemaDivergenceFor(simple('us.anthropic.claude-haiku-5-5'), {}, 'extraction').forcedTool).toBe(true);
      // An unset model keeps the tab: the backend decides, this only affects the preview.
      expect(schemaDivergenceFor(forcing({ mode: 'simple' }), {}, 'extraction').forcedTool).toBe(true);
    });
  });

  describe('restatesSchema', () => {
    it('DEFAULTS TRUE in Advanced mode, so an absent key means the block is sent', () => {
      // The mirror image of every other flag here: restate_schema_in_system_prompt
      // defaults True server-side, so treating "absent" as off would hide ~40% of
      // the real system prompt.
      expect(schemaDivergenceFor({ extraction: { mode: 'advanced' } }, {}, 'extraction').restatesSchema).toBe(true);
      expect(schemaDivergenceFor({ extraction: { mode: 'advanced', agentic: {} } }, {}, 'extraction').restatesSchema).toBe(true);
      expect(
        schemaDivergenceFor({ extraction: { mode: 'advanced', agentic: { restate_schema_in_system_prompt: null } } }, {}, 'extraction')
          .restatesSchema,
      ).toBe(true);
    });

    it('is off when explicitly disabled, in any spelling pydantic accepts', () => {
      for (const v of [false, 0, 'false', 'no', 'off', 'N']) {
        expect(
          schemaDivergenceFor({ extraction: { mode: 'advanced', agentic: { restate_schema_in_system_prompt: v } } }, {}, 'extraction')
            .restatesSchema,
        ).toBe(false);
      }
    });

    it('is off in Simple mode, which has no agent and no system-prompt restatement', () => {
      expect(schemaDivergenceFor({ extraction: { mode: 'simple' } }, {}, 'extraction').restatesSchema).toBe(false);
      expect(
        schemaDivergenceFor(
          { extraction: { mode: 'simple', agentic: { enabled: true, restate_schema_in_system_prompt: true } } },
          {},
          'extraction',
        ).restatesSchema,
      ).toBe(false);
    });

    it('never leaks into the confidence step', () => {
      expect(schemaDivergenceFor({ extraction: { mode: 'advanced' } }, {}, 'confidence').restatesSchema).toBe(false);
    });
  });
});

describe('simpleIntegratedDowngraded', () => {
  const simpleIntegrated = { extraction: { mode: 'simple', confidence: { mode: 'integrated' } } };
  const listClass = { $id: 'Stmt', properties: { Rows: { type: 'array', items: { type: 'object' } } } };
  const scalarClass = { $id: 'Card', properties: { Id: { type: 'string' } } };

  it('is on for Simple + integrated on a list-bearing or multi-instance class', () => {
    expect(simpleIntegratedDowngraded(simpleIntegrated, listClass)).toBe(true);
    expect(simpleIntegratedDowngraded(simpleIntegrated, { ...scalarClass, 'x-aws-idp-multi-instance': true })).toBe(true);
  });

  it('is off for scalar-only classes, Advanced mode, other confidence modes and disabled confidence', () => {
    expect(simpleIntegratedDowngraded(simpleIntegrated, scalarClass)).toBe(false);
    expect(simpleIntegratedDowngraded({ extraction: { mode: 'advanced', confidence: { mode: 'integrated' } } }, listClass)).toBe(false);
    expect(simpleIntegratedDowngraded({ extraction: { mode: 'simple', confidence: { mode: 'separate' } } }, listClass)).toBe(false);
    const disabled = { extraction: { mode: 'simple', confidence: { mode: 'integrated', enabled: false } } };
    expect(simpleIntegratedDowngraded(disabled, listClass)).toBe(false);
    expect(simpleIntegratedDowngraded(simpleIntegrated, null)).toBe(false);
  });

  it('honours both opt-outs: a per-class task prompt and x-aws-idp-allow-integrated-lists', () => {
    const overridden = { ...listClass, 'x-aws-idp-extraction-task-prompt': 'MY {DOCUMENT_TEXT}' };
    expect(simpleIntegratedDowngraded(simpleIntegrated, overridden)).toBe(false);
    expect(simpleIntegratedDowngraded(simpleIntegrated, { ...listClass, 'x-aws-idp-allow-integrated-lists': true })).toBe(false);
    expect(simpleIntegratedDowngraded(simpleIntegrated, { ...listClass, 'x-aws-idp-allow-integrated-lists': 'true' })).toBe(false);
    expect(simpleIntegratedDowngraded(simpleIntegrated, { ...listClass, 'x-aws-idp-allow-integrated-lists': false })).toBe(true);
    // String spellings agree with the backend's flag_is_true: "false" is NOT an opt-in.
    expect(simpleIntegratedDowngraded(simpleIntegrated, { ...listClass, 'x-aws-idp-allow-integrated-lists': 'false' })).toBe(true);
    expect(simpleIntegratedDowngraded(simpleIntegrated, { ...scalarClass, 'x-aws-idp-multi-instance': 'false' })).toBe(false);
  });

  it('treats mode as authoritative over a stale agentic.enabled, like the backend', () => {
    const stale = { extraction: { mode: 'simple', agentic: { enabled: true }, confidence: { mode: 'integrated' } } };
    expect(simpleIntegratedDowngraded(stale, listClass)).toBe(true);
  });
});

describe('extractionModeOf', () => {
  it('treats mode as authoritative and falls back to agentic.enabled', () => {
    expect(extractionModeOf({ extraction: { mode: 'advanced', agentic: { enabled: false } } })).toBe('advanced');
    expect(extractionModeOf({ extraction: { mode: 'simple', agentic: { enabled: true } } })).toBe('simple');
    expect(extractionModeOf({ extraction: { agentic: { enabled: true } } })).toBe('advanced');
    expect(extractionModeOf({ extraction: {} })).toBe('simple');
    expect(extractionModeOf(undefined)).toBe('simple');
  });
});

/**
 * The forced toolSpec. These pin the transforms the browser CAN reproduce; the
 * property-name sanitizer is deliberately not ported (see `toolInputSchemaFor`),
 * so what is pinned about renaming is only that the UI can COUNT the names that
 * get renamed.
 */
describe('toolInputSchemaFor', () => {
  it('strips x-aws-idp-* extensions and schema-document keywords at every depth', () => {
    const out = toolInputSchemaFor({
      $schema: 'https://json-schema.org/draft/2020-12/schema',
      $id: 'Invoice',
      $comment: 'authored by hand',
      type: 'object',
      'x-aws-idp-document-type': 'Invoice',
      properties: {
        total: { type: 'number', $id: 'nested', 'x-aws-idp-evaluation-method': 'EXACT' },
      },
      $defs: { Line: { $anchor: 'line', type: 'object', properties: { sku: { type: 'string' } } } },
    });

    expect(out).toEqual({
      type: 'object',
      properties: { total: { type: 'number' } },
      $defs: { Line: { type: 'object', properties: { sku: { type: 'string' } } } },
    });
  });

  it('keeps a PROPERTY legitimately named like a metadata keyword', () => {
    // strip_non_wire_keywords never filters inside `properties`, because those
    // keys are user-authored field names.
    const out = toolInputSchemaFor({ type: 'object', properties: { id: { type: 'string' }, $schema: { type: 'string' } } });
    expect(Object.keys(out.properties as Record<string, unknown>)).toEqual(['id', '$schema']);
  });

  it('forces an object root, as build_extraction_tool_config does', () => {
    expect(toolInputSchemaFor({ properties: { a: { type: 'string' } } }).type).toBe('object');
  });
});

describe('invalidToolPropertyNames', () => {
  it('finds every name Bedrock would reject, at every depth', () => {
    expect(
      invalidToolPropertyNames({
        properties: {
          'Invoice Number': { type: 'string' },
          ok_name: { type: 'string' },
          group: { type: 'object', properties: { 'Total (USD)': { type: 'number' } } },
          rows: { type: 'array', items: { type: 'object', properties: { 'Line Item': { type: 'string' } } } },
        },
        $defs: { Fee: { type: 'object', properties: { 'Fee Amount': { type: 'number' } } } },
      }),
    ).toEqual(['Invoice Number', 'group.Total (USD)', 'rows[].Line Item', '$defs/Fee.Fee Amount']);
  });

  it('reports nothing for a schema Bedrock already accepts', () => {
    expect(invalidToolPropertyNames({ properties: { a_b: { type: 'string' }, 'c.d-e': { type: 'string' } } })).toEqual([]);
  });

  it('rejects a name over 64 characters, which the backend truncates', () => {
    expect(invalidToolPropertyNames({ properties: { ['x'.repeat(65)]: { type: 'string' } } })).toHaveLength(1);
  });
});

describe('toolSpecWireText', () => {
  const cls = { $id: 'Invoice', type: 'object', properties: { total: { type: 'number', description: 'The total' } } };

  it('carries the tool name and the verbatim description', () => {
    const spec = JSON.parse(toolSpecWireText(cls));
    expect(spec.name).toBe(EXTRACTION_TOOL_NAME);
    expect(spec.description).toContain('Do NOT nest them under a wrapper key');
    expect(spec.inputSchema.json.properties.total.description).toBe('The total');
  });

  it('is the compact form, so the token estimate is not inflated by display indentation', () => {
    // The wire form is what the model is billed for; the pretty form is a choice
    // of this component. Counting the pretty one overstates Schema Enforcement.
    expect(toolSpecWireText(cls)).not.toContain('\n');
    expect(toolSpecWireText(cls).length).toBeLessThan(formatToolSpecForPreview(cls).length);
  });
});

describe('expectedSchemaBlockFor', () => {
  it('reproduces the label the agentic path prepends, over the cleaned class schema', () => {
    const block = expectedSchemaBlockFor({ $id: 'Invoice', type: 'object', properties: { a: { type: 'string' } } });
    expect(block.startsWith('Expected Schema:\n')).toBe(true);
    expect(JSON.parse(block.slice('Expected Schema:\n'.length))).toEqual({
      $id: 'Invoice',
      type: 'object',
      properties: { a: { type: 'string' } },
    });
  });
});

/**
 * Switch the Processing Step select (the preview opens on the first step the
 * current strategy makes available). Cloudscape's Select trigger opens on a
 * real pointer sequence, not a bare click event, so this goes through
 * userEvent.
 */
const showStep = async (label: string): Promise<void> => {
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { expanded: false, name: /Processing Step/i }));
  await user.click(await screen.findByRole('option', { name: new RegExp(`^${label}$`) }));
};

/**
 * Open a tab and return the rendered PROMPT BODY, not the page text. The
 * warning alert also mentions "Expected Schema:", so asserting on the whole
 * document would pass on the alert alone and never look at the prompt.
 */
const promptBody = async (tabLabel: RegExp): Promise<string> => {
  await userEvent.setup().click(screen.getByText(tabLabel));
  const body = document.querySelector('.prompt-preview-content');
  if (!body) throw new Error('no prompt body rendered');
  return body.textContent ?? '';
};

/**
 * End-to-end on the two things a user actually reads: whether a Tool Schema tab
 * exists, and whether the token total moves when they flip the knob. Both were
 * silently wrong before — turning Schema Enforcement on changed the previewed
 * total by exactly 0, which told the one number people come to this page for that
 * forcing is free.
 */
describe('PromptPreview token accounting', () => {
  const CLASS = {
    $id: 'Invoice',
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    type: 'object',
    description: 'A commercial invoice',
    properties: {
      'Invoice Number': { type: 'string', description: 'The invoice identifier', 'x-aws-idp-evaluation-method': 'EXACT' },
      total: { type: 'number', description: 'Amount due' },
    },
  };

  const configWith = (extraction: Record<string, unknown>) => ({
    classes: [CLASS],
    extraction: {
      model: 'us.anthropic.claude-sonnet-5',
      system_prompt: 'You are a document extraction expert.',
      task_prompt: 'Extract {DOCUMENT_CLASS} using {ATTRIBUTE_NAMES_AND_DESCRIPTIONS} from {DOCUMENT_TEXT}',
      ...extraction,
    },
  });

  const showExtractionStep = (): Promise<void> => showStep('Extraction');

  const totalTokens = (): number => {
    const row = screen.getByText('Total (est.)').closest('.prompt-stat-item');
    const match = /~([\d,]+) tokens/.exec(row?.textContent ?? '');
    if (!match) throw new Error(`no total in ${row?.textContent}`);
    return Number(match[1].replace(/,/g, ''));
  };

  it('Simple with forcing off is unchanged: no tab, no tool-schema stat', async () => {
    render(<PromptPreview formValues={configWith({ mode: 'simple' })} />);
    await showExtractionStep();
    expect(screen.queryByText(/^Tool Schema/)).toBeNull();
    expect(screen.queryByText('Tool schema')).toBeNull();
    expect(totalTokens()).toBeGreaterThan(0);
  });

  it('Simple with forcing on adds the tab, the stat and real tokens to the total', async () => {
    const { unmount } = render(<PromptPreview formValues={configWith({ mode: 'simple' })} />);
    await showExtractionStep();
    const baseline = totalTokens();
    unmount();

    render(<PromptPreview formValues={configWith({ mode: 'simple', forced_tool: { enabled: true } })} />);
    await showExtractionStep();
    expect(screen.getByText(/^Tool Schema \(~\d+ tokens\)$/)).toBeTruthy();
    expect(screen.getByText('Tool schema')).toBeTruthy();
    expect(totalTokens()).toBeGreaterThan(baseline);
    // The alert explains the renaming, since this class has a name with a space.
    expect(screen.getByText(/rewritten to a wire-safe spelling/)).toBeTruthy();
  });

  it('Advanced restates the schema by DEFAULT, growing the system prompt and the total', async () => {
    // No restate_schema_in_system_prompt key at all — the default is ON.
    const first = render(<PromptPreview formValues={configWith({ mode: 'advanced' })} />);
    await showExtractionStep();
    const withRestatement = totalTokens();
    const restated = await promptBody(/^System Prompt \(~/);
    expect(restated).toContain('Expected Schema:');
    // ...and it is the class schema, cleaned, not a stub.
    expect(restated).toContain('"Invoice Number"');
    expect(restated).not.toContain('x-aws-idp-evaluation-method');
    first.unmount();

    render(<PromptPreview formValues={configWith({ mode: 'advanced', agentic: { restate_schema_in_system_prompt: false } })} />);
    await showExtractionStep();
    expect(await promptBody(/^System Prompt \(~/)).not.toContain('Expected Schema:');
    expect(totalTokens()).toBeLessThan(withRestatement);
  });

  it('Advanced with the forced-tool knob on still shows no Tool Schema tab', async () => {
    render(<PromptPreview formValues={configWith({ mode: 'advanced', forced_tool: { enabled: true } })} />);
    await showExtractionStep();
    expect(screen.queryByText(/^Tool Schema/)).toBeNull();
  });

  it('neither block reaches the Confidence Assessment step', async () => {
    render(
      <PromptPreview
        formValues={{
          ...configWith({ mode: 'simple', forced_tool: { enabled: true }, confidence: { system_prompt: 'sys', task_prompt: 'task' } }),
        }}
      />,
    );
    await showStep('Confidence Assessment');
    expect(screen.queryByText(/^Tool Schema/)).toBeNull();
    expect(await promptBody(/^System Prompt \(~/)).not.toContain('Expected Schema:');
  });

  it('does not put the restatement in the RAW system template, which is the user’s text', async () => {
    render(<PromptPreview formValues={configWith({ mode: 'advanced' })} />);
    await showExtractionStep();
    // The rendered System tab holds the appended block...
    expect(await promptBody(/^System Prompt \(~/)).toContain('Expected Schema:');
    // ...but the RAW template tab must show only what the user typed, since that
    // is the text they edit. A block appended by the runtime is not part of it.
    expect(await promptBody(/^Raw System Template$/)).toBe('You are a document extraction expert.');
  });
});

// ─── Classification prompt resolution ────────────────────────────────────────
//
// The prompt a classification call actually sends is a function of FOUR settings
// — granularity, strategy, modality and confidence.mode — so a preview showing
// the raw `classification.task_prompt` shows a prompt that will not be sent.
// That is worse than showing nothing, because it looks authoritative.
//
// These mirror the Python in `ClassificationService`, and the point of testing
// them separately is that the two implementations can drift: a change to
// `_apply_modality_to_prompt` with no change here leaves the preview confidently
// wrong, which is exactly how the old `classificationMethod` dropdown came to
// offer a method that failed every document.

describe('applyModalityToPrompt', () => {
  const PROMPT = '<document-ocr-data>\n{DOCUMENT_TEXT}\n</document-ocr-data>\n' + '<document-image>\n{DOCUMENT_IMAGE}\n</document-image>\n';

  it('leaves the prompt alone when both flags are unset', () => {
    // ⚠️ The common case by far. An unset flag is INFERRED from the
    // placeholders, so the prompt already agrees with it and rewriting would
    // misrepresent what runs.
    expect(applyModalityToPrompt(PROMPT, {})).toBe(PROMPT);
    expect(applyModalityToPrompt(PROMPT, { text: '', images: '' })).toBe(PROMPT);
  });

  it('leaves the prompt alone when an explicit flag agrees with it', () => {
    expect(applyModalityToPrompt(PROMPT, { text: 'true', images: 'true' })).toBe(PROMPT);
  });

  it('removes the image placeholder and its wrapper when images are off', () => {
    const out = applyModalityToPrompt(PROMPT, { images: 'false' });
    expect(out).not.toContain('{DOCUMENT_IMAGE}');
    // An empty element announcing a switched-off modality is misleading, so the
    // wrapper goes too.
    expect(out).not.toContain('<document-image>');
    expect(out).toContain('{DOCUMENT_TEXT}');
  });

  it('removes the text placeholder and its wrapper when text is off', () => {
    const out = applyModalityToPrompt(PROMPT, { text: 'false' });
    expect(out).not.toContain('{DOCUMENT_TEXT}');
    expect(out).not.toContain('<document-ocr-data>');
    expect(out).toContain('{DOCUMENT_IMAGE}');
  });

  it('appends a placeholder when a flag is on and the prompt has none', () => {
    const out = applyModalityToPrompt('{DOCUMENT_TEXT}', { images: 'true' });
    expect(out).toContain('{DOCUMENT_IMAGE}');
    expect(out.indexOf('{DOCUMENT_TEXT}')).toBeLessThan(out.indexOf('{DOCUMENT_IMAGE}'));
  });

  it('deletes an unwrapped placeholder', () => {
    expect(applyModalityToPrompt('before {DOCUMENT_IMAGE} after', { images: 'false' })).toBe('before  after');
  });

  it('accepts a real boolean as well as the string form', () => {
    // The form holds booleans while a stored config deserializes as strings.
    expect(applyModalityToPrompt(PROMPT, { images: false })).not.toContain('{DOCUMENT_IMAGE}');
    expect(applyModalityToPrompt(PROMPT, { images: true })).toBe(PROMPT);
  });
});

describe('appendClassConfidenceBlock', () => {
  const BLOCK = '<class-confidence>top {TOP_K_CANDIDATES}</class-confidence>';

  it('splices before the cache point so the block stays cacheable', () => {
    const core = 'instructions\n<<CACHEPOINT>>\n{DOCUMENT_TEXT}';
    const out = appendClassConfidenceBlock(core, BLOCK, 3);
    expect(out.indexOf('top 3')).toBeLessThan(out.indexOf('<<CACHEPOINT>>'));
  });

  it('falls back to the document marker when there is no cache point', () => {
    const out = appendClassConfidenceBlock('a\n{DOCUMENT_TEXT}', BLOCK, 3);
    expect(out.indexOf('top 3')).toBeLessThan(out.indexOf('{DOCUMENT_TEXT}'));
  });

  it('appends when the prompt has no marker at all', () => {
    expect(appendClassConfidenceBlock('just text', BLOCK, 2)).toContain('top 2');
  });

  it('is idempotent, so a prompt that spells the block out itself is left alone', () => {
    const core = 'mine <class-confidence>my own wording</class-confidence>';
    expect(appendClassConfidenceBlock(core, BLOCK, 3)).toBe(core);
  });

  it('does nothing with an empty block', () => {
    expect(appendClassConfidenceBlock('core', '', 3)).toBe('core');
  });

  it('leaves the placeholder alone for the verbalized block, which has no count', () => {
    const out = appendClassConfidenceBlock('core', '<class-confidence>one number</class-confidence>', null);
    expect(out).toContain('one number');
  });
});

describe('resolveTopK', () => {
  it('never asks for more candidates than there are classes', () => {
    // Asking for 5 from a 3-class vocabulary invites the model to invent
    // classes to fill the list.
    expect(resolveTopK(5, 3)).toBe(3);
  });

  it('never asks for fewer than two', () => {
    // One candidate is a verbalized confidence with extra syntax; the
    // calibration benefit comes from having to rank alternatives.
    expect(resolveTopK(1, 10)).toBe(2);
    expect(resolveTopK(5, 1)).toBe(2);
  });

  it('passes a sensible request through', () => {
    expect(resolveTopK(3, 13)).toBe(3);
  });

  it('falls back to the configured count when there are no classes', () => {
    expect(resolveTopK(4, 0)).toBe(4);
  });
});

// Config format 0.9: each classification call carries its own prompts, model,
// modality and confidence. The preview must read the call the selected step is,
// never a neighbour's setting.
describe('resolveClassificationStepPrompt', () => {
  const TOPK = '<class-confidence>top {TOP_K_CANDIDATES}</class-confidence>';
  const TOPK_PACKET = '<class-confidence>per segment, top {TOP_K_CANDIDATES}</class-confidence>';
  const cls = {
    combined: {
      granularity: 'page',
      page: { system_prompt: 'PAGE SYS', task_prompt: 'PAGE {DOCUMENT_TEXT} {DOCUMENT_IMAGE}' },
      packet: { system_prompt: 'PACKET SYS', task_prompt: 'PACKET {DOCUMENT_TEXT}' },
      llm: {
        model: 'combined-model',
        modality: { images: 'false' },
        confidence: { mode: 'topk', top_k_candidates: 3, task_prompt_topk: TOPK, task_prompt_topk_packet: TOPK_PACKET },
      },
    },
    split_then_classify: {
      splitting: {
        llm: {
          model: 'splitting-model',
          granularity: 'packet',
          page: { task_prompt: 'SEG PAGE {DOCUMENT_TEXT}' },
          packet: { system_prompt: 'SEG SYS', task_prompt: 'SEG PACKET {DOCUMENT_TEXT}' },
        },
      },
      section_classification: {
        llm: {
          model: 'section-model',
          system_prompt: 'SC SYS',
          task_prompt: 'SC {DOCUMENT_TEXT}',
          confidence: { mode: 'topk', top_k_candidates: 3, task_prompt_topk: TOPK, task_prompt_topk_packet: TOPK_PACKET },
        },
      },
    },
  };

  it('the combined call reads its own granularity, prompts, model and modality', () => {
    const out = resolveClassificationStepPrompt(cls, 'classification', 5);
    expect(out.model).toBe('combined-model');
    expect(out.system_prompt).toBe('PAGE SYS');
    expect(out.task_prompt).not.toContain('{DOCUMENT_IMAGE}');
    expect(out.task_prompt).toContain('top 3');
  });

  it('a combined packet call gets the packet prompt and the per-segment block', () => {
    const packet = { ...cls, combined: { ...cls.combined, granularity: 'packet' } };
    const out = resolveClassificationStepPrompt(packet, 'classification', 5);
    expect(out.system_prompt).toBe('PACKET SYS');
    expect(out.task_prompt).toContain('PACKET');
    expect(out.task_prompt).toContain('per segment, top 3');
  });

  it('splitting reads its own granularity and model, and asks for no confidence', () => {
    const out = resolveClassificationStepPrompt(cls, 'classification_splitting', 5);
    expect(out.model).toBe('splitting-model');
    expect(out.system_prompt).toBe('SEG SYS');
    expect(out.task_prompt).toBe('SEG PACKET {DOCUMENT_TEXT}');
  });

  it('section classification takes the page-shaped block and keeps the images the combined call turned off', () => {
    const out = resolveClassificationStepPrompt(cls, 'classification_section', 5);
    expect(out.model).toBe('section-model');
    expect(out.system_prompt).toBe('SC SYS');
    expect(out.task_prompt).toContain('top 3');
    expect(out.task_prompt).not.toContain('per segment');
  });
});

/**
 * Which placeholders the preview leaves empty for which step.
 *
 * This is the UI half of a guarantee whose Python half is enforced in
 * `lib/idp_common_pkg/tests/unit/classification/test_split_then_classify.py`: a
 * split-then-classify splitting call carries no class vocabulary and no examples,
 * and `{BOUNDARY_HINTS}` is rendered for splitting and nowhere else. The preview
 * restates those rules, so it can disagree with the service — and the preview
 * is where an operator writing a custom prompt would discover the rule, so a
 * preview promising content the call will not send is worse than no preview.
 *
 * Both assertions go through a real render rather than through a helper,
 * because `buildSubstitutions` is not exported; the step select and the prompt
 * body are the only seams onto it.
 */
describe('PromptPreview placeholder scoping by step', () => {
  const A_DESCRIPTION = 'a distinctive letterhead';
  const AN_ATTRIBUTE = 'total_amount_due';
  /** The marker DOCUMENT_PLACEHOLDER_LABELS substitutes when examples ARE sent. */
  const FEW_SHOT_MARKER = 'Few-shot examples from class configuration';

  const CLASSES = [
    {
      $id: 'Invoice',
      $schema: 'https://json-schema.org/draft/2020-12/schema',
      type: 'object',
      description: A_DESCRIPTION,
      properties: { [AN_ATTRIBUTE]: { type: 'string', description: 'Amount due' } },
    },
  ];

  /** A template asking for every placeholder whose scope is under test. */
  const TEMPLATE = [
    'Where does each document start?',
    '{BOUNDARY_HINTS}',
    '<types>{CLASS_NAMES_AND_DESCRIPTIONS}</types>',
    '<attrs>{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}</attrs>',
    '<examples>{FEW_SHOT_EXAMPLES}</examples>',
    '<ocr>{DOCUMENT_TEXT}</ocr>',
  ].join('\n');

  const PROMPTS = { system_prompt: 'sys', task_prompt: TEMPLATE };
  const OFF = { model: 'us.amazon.nova-2-lite-v1:0', confidence: { mode: 'off' } };

  const configFor = (strategy: string) => ({
    classes: CLASSES,
    classification: {
      strategy,
      combined: { granularity: 'page', page: PROMPTS, llm: OFF },
      split_then_classify: {
        // `class_names` on purpose: with `none` the hints block is empty for
        // every step and the combined-step assertion below would hold whether or
        // not the preview scopes it.
        splitting: { llm: { granularity: 'page', boundary_hints: 'class_names', page: PROMPTS, ...OFF } },
        section_classification: { llm: { ...PROMPTS, ...OFF } },
      },
    },
  });

  it('a splitting step previews no class vocabulary and no examples', async () => {
    render(<PromptPreview formValues={configFor('split_then_classify')} />);
    await showStep('Classification: splitting \\(split then classify\\)');

    const body = await promptBody(/^Task Prompt \(~/);
    expect(body).not.toContain(A_DESCRIPTION);
    expect(body).not.toContain(AN_ATTRIBUTE);
    expect(body).not.toContain(FEW_SHOT_MARKER);
    // No literal tokens left behind either: empty is not the same as unreplaced.
    expect(body).not.toContain('{CLASS_NAMES_AND_DESCRIPTIONS}');
    expect(body).not.toContain('{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}');
    expect(body).not.toContain('{FEW_SHOT_EXAMPLES}');
    // The one vocabulary a splitting call may carry, and it is asked for here.
    expect(body).toContain('document-type-hints');
  });

  it('a combined step previews no boundary-hints block', async () => {
    // `classification` is the only classification step a combined config
    // offers, so the preview opens on it.
    render(<PromptPreview formValues={configFor('combined')} />);

    const body = await promptBody(/^Task Prompt \(~/);
    expect(body).not.toContain('document-type-hints');
    expect(body).not.toContain('{BOUNDARY_HINTS}');
    // Rendered, not blank: a combined call does carry the vocabulary.
    expect(body).toContain(A_DESCRIPTION);
    expect(body).toContain(FEW_SHOT_MARKER);
  });

  it('a section-classification step previews no boundary-hints block either', async () => {
    render(<PromptPreview formValues={configFor('split_then_classify')} />);
    await showStep('Classification: section classification \\(split then classify\\)');

    const body = await promptBody(/^Task Prompt \(~/);
    expect(body).not.toContain('document-type-hints');
    expect(body).not.toContain('{BOUNDARY_HINTS}');
    expect(body).toContain(A_DESCRIPTION);
  });
});
