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
  EXTRACTION_TOOL_NAME,
  expectedSchemaBlockFor,
  extractionModeOf,
  formatToolSpecForPreview,
  getAttributeNamesForClass,
  invalidToolPropertyNames,
  schemaDivergenceFor,
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

  /**
   * Switch the Processing Step select (the preview opens on Classification).
   * Cloudscape's Select trigger opens on a real pointer sequence, not a bare
   * click event, so this goes through userEvent.
   */
  const showStep = async (label: string): Promise<void> => {
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { expanded: false, name: /Processing Step/i }));
    await user.click(await screen.findByRole('option', { name: new RegExp(`^${label}$`) }));
  };
  const showExtractionStep = (): Promise<void> => showStep('Extraction');

  const totalTokens = (): number => {
    const row = screen.getByText('Total (est.)').closest('.prompt-stat-item');
    const match = /~([\d,]+) tokens/.exec(row?.textContent ?? '');
    if (!match) throw new Error(`no total in ${row?.textContent}`);
    return Number(match[1].replace(/,/g, ''));
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
