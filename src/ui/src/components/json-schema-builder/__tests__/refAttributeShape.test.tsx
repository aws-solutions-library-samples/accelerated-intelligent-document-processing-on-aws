// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * One shape for a `$ref` attribute, whichever route creates it (GitHub #957).
 *
 * A property can be turned into a reference to a shared class two ways: the Add
 * Attribute modal, picking a type of Object and then a class; or the inspector's
 * "Reference Existing Class" picker on a property that already exists. The two
 * wrote different JSON for the same user-visible action — one a bare `$ref`, the
 * other a `$ref` with a sibling `type: 'object'` — which is what made the empty
 * Evaluation Method dropdown of #906 look intermittent.
 *
 * The first test drives both routes through the real components against one
 * designer and compares the exported JSON, so a future edit to either route that
 * changes the shape fails here rather than surfacing as an intermittent bug.
 *
 * Why the shape is the bare `$ref`: the referenced `$defs` entry declares the
 * type, and the backend derives it by following the pointer — `tool_schema.py`
 * annotates outgoing `$ref` nodes with the target's real type — so there is
 * nothing for the designer to supply. It also stays correct *if* a `$ref` to a
 * non-object definition ever appears, which the designer cannot currently hold:
 * `convertJsonSchemaToClasses` builds every `$defs` entry as `type: 'object'`
 * and `exportSchema` writes every one back the same way.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, type Mock } from 'vitest';
import type { ComponentProps } from 'react';
import SchemaBuilder from '../SchemaBuilder';
import SchemaInspector from '../SchemaInspector';
import { refAttributeUpdates, sanitizeAttribute } from '../utils/schemaHelpers';
import { X_AWS_IDP_DOCUMENT_TYPE } from '../../../constants/schemaConstants';

type Json = Record<string, unknown>;
type BuilderSchema = NonNullable<ComponentProps<typeof SchemaBuilder>['initialSchema']>;
type InspectorClass = NonNullable<ComponentProps<typeof SchemaInspector>['availableClasses']>[number];
type OnUpdate = ComponentProps<typeof SchemaInspector>['onUpdate'];

/** The shared class both routes point at. */
const ADDRESS_CLASS: InspectorClass = {
  id: 'class-address',
  name: 'Address',
  [X_AWS_IDP_DOCUMENT_TYPE]: false,
  attributes: {
    properties: { street: { type: 'string', description: '' } },
    required: [],
  },
};

/**
 * A document type with one inline-object property, plus the shared class.
 * `attributes.type` is how `addClass` records a class body, so the fixture keeps
 * it even though the prop type does not name it.
 */
const designerState = (): BuilderSchema =>
  [
    {
      id: 'class-invoice',
      name: 'Invoice',
      [X_AWS_IDP_DOCUMENT_TYPE]: true,
      attributes: {
        type: 'object',
        properties: {
          vendorName: { type: 'string', description: '' },
          shipsTo: { type: 'object', description: 'Where the goods go', properties: {}, required: [] },
        },
        required: [],
      },
    },
    { ...ADDRESS_CLASS, attributes: { type: 'object', ...ADDRESS_CLASS.attributes } },
  ] as unknown as BuilderSchema;

describe('the two routes to a $ref attribute (#957)', () => {
  it('produce identical JSON for the same class selection', async () => {
    const user = userEvent.setup();
    const exported: Json[][] = [];
    render(<SchemaBuilder initialSchema={designerState()} onChange={(schema) => exported.push(schema as Json[])} />);

    /** Properties of the exported Invoice schema, as last reported to `onChange`. */
    const properties = (): Json => (exported[exported.length - 1]?.[0]?.properties as Json) ?? {};

    // Route 1 — the Add Attribute modal, with Object + the Address class.
    await user.click(screen.getAllByRole('button', { name: 'Add Attribute' })[0]);
    const modal = screen.getByRole('dialog', { name: 'Add Attribute' });
    await user.type(within(modal).getByPlaceholderText(/invoiceNumber/), 'billTo');
    await user.type(within(modal).getByPlaceholderText(/unique invoice number/), 'Where the goods go');
    await user.click(within(modal).getByRole('button', { expanded: false, name: /Attribute Type/i }));
    await user.click(await screen.findByRole('option', { name: /^Object$/ }));
    await user.click(within(modal).getByRole('button', { expanded: false, name: /Object Type/i }));
    await user.click(await screen.findByRole('option', { name: /Address/ }));
    await user.click(within(modal).getByRole('button', { name: 'Add Attribute' }));
    await waitFor(() => expect(properties()).toHaveProperty('billTo'));

    // Route 2 — select the existing inline object, then the inspector's picker.
    await user.click(screen.getByText('shipsTo'));
    await user.click(screen.getByRole('button', { expanded: false, name: /Reference Existing Class/i }));
    await user.click(await screen.findByRole('option', { name: /^Address$/ }));
    await waitFor(() => expect(properties().shipsTo).toHaveProperty('$ref'));

    // Same selection, same JSON — including the absence of `type`.
    expect(properties().billTo).toEqual(properties().shipsTo);
    expect(properties().billTo).toEqual({ $ref: '#/$defs/Address', description: 'Where the goods go' });
    expect(Object.keys(properties().shipsTo as Json)).not.toContain('type');
  });
});

describe("the inspector's Reference Existing Class picker", () => {
  const INVOICE: InspectorClass = { id: 'class-1', name: 'Invoice', attributes: { properties: {}, required: [] } };

  /**
   * The payload, not the exported schema, is where this is observable: the
   * exporter strips a `type` sitting beside a `$ref`, so both shapes have always
   * serialized the same. What differed is the node the designer then holds and
   * reads back — and `resolveAttributeType` prefers a sibling `type` over the
   * pointer, so a spurious `'object'` is what the inspector believes afterwards.
   */
  it('clears the inline type rather than pinning it to object', async () => {
    const user = userEvent.setup();
    const onUpdate: Mock<OnUpdate> = vi.fn();
    render(
      <SchemaInspector
        selectedClass={INVOICE}
        selectedAttribute={{ type: 'object', description: 'Where the goods go' }}
        selectedAttributeName="shipsTo"
        onUpdate={onUpdate}
        availableClasses={[ADDRESS_CLASS]}
      />,
    );

    await user.click(screen.getByRole('button', { expanded: false, name: /Reference Existing Class/i }));
    await user.click(await screen.findByRole('option', { name: /^Address$/ }));

    expect(onUpdate).toHaveBeenCalledWith(refAttributeUpdates('#/$defs/Address'));
  });

  it('restores an inline object type when the reference is removed', async () => {
    const user = userEvent.setup();
    const onUpdate: Mock<OnUpdate> = vi.fn();
    render(
      <SchemaInspector
        selectedClass={INVOICE}
        selectedAttribute={{ $ref: '#/$defs/Address', description: 'Where the goods go' }}
        selectedAttributeName="shipsTo"
        onUpdate={onUpdate}
        availableClasses={[ADDRESS_CLASS]}
      />,
    );

    await user.click(screen.getByRole('button', { expanded: false, name: /Reference Existing Class/i }));
    await user.click(await screen.findByRole('option', { name: /Inline properties|^None/ }));

    // Nothing else declares the type once the pointer is gone.
    expect(onUpdate).toHaveBeenCalledWith({ $ref: undefined, type: 'object' });
  });

  /**
   * A forward guard on a constructed fixture, not a state this product can reach:
   * `convertJsonSchemaToClasses` flattens every imported `$defs` entry to
   * `type: 'object'` and `exportSchema` writes them all back that way, so the
   * designer cannot hold a class standing for a non-object definition today.
   *
   * It is worth pinning because of what a mismatch costs if that changes, measured
   * against the backend: stamping `type: 'object'` onto a reference to a scalar
   * definition makes the Pydantic generator drop the `$ref` and yield an
   * unconstrained `dict[str, Any]`, and makes the tool schema tell the model the
   * field is an object. Both fail silently.
   */
  it('adds no contradictory type for a reference to a non-object definition', async () => {
    const scalarClass = {
      id: 'class-code',
      name: 'StateCode',
      attributes: { type: 'string', properties: {}, required: [] },
    } as unknown as InspectorClass;
    const user = userEvent.setup();
    const onUpdate: Mock<OnUpdate> = vi.fn();
    render(
      <SchemaInspector
        selectedClass={INVOICE}
        selectedAttribute={{ type: 'object', description: '' }}
        selectedAttributeName="state"
        onUpdate={onUpdate}
        availableClasses={[scalarClass]}
      />,
    );

    await user.click(screen.getByRole('button', { expanded: false, name: /Reference Existing Class/i }));
    await user.click(await screen.findByRole('option', { name: /^StateCode$/ }));

    expect(onUpdate).toHaveBeenCalledWith(refAttributeUpdates('#/$defs/StateCode'));
    expect(onUpdate.mock.calls[0][0].type).toBeUndefined();
  });
});

describe('refAttributeUpdates', () => {
  it('names the reference and clears every keyword that describes an inline object', () => {
    expect(refAttributeUpdates('#/$defs/Address')).toEqual({
      $ref: '#/$defs/Address',
      type: undefined,
      properties: undefined,
      required: undefined,
      minProperties: undefined,
      maxProperties: undefined,
      additionalProperties: undefined,
    });
  });

  it('clears by explicit undefined, which is how updateAttribute deletes a key', () => {
    // An omitted key means "leave it alone" to `updateAttribute`, so a keyword has to
    // be present and undefined to go. This is load-bearing beyond `type`: the previous
    // inspector code built a copy of the attribute and `delete`d `properties` and
    // `required` from it, which left both on the stored attribute as orphan state
    // underneath the `$ref`.
    const updates = refAttributeUpdates('#/$defs/Address');
    for (const keyword of ['type', 'properties', 'required']) {
      expect(Object.keys(updates)).toContain(keyword);
      expect(updates[keyword]).toBeUndefined();
    }
  });
});

describe('sanitizeAttribute', () => {
  it('drops the inline-object keywords that cannot sit beside a $ref', () => {
    // Normalizes a node that acquired a stray `type` elsewhere — an older saved
    // schema, or a hand edit — the same way the live export path does.
    expect(sanitizeAttribute({ id: 'a1', name: 'shipsTo', $ref: '#/$defs/Address', type: 'object', properties: {}, required: [] })).toEqual(
      { $ref: '#/$defs/Address' },
    );
  });

  it('leaves the type of a property that has no $ref alone', () => {
    expect(sanitizeAttribute({ id: 'a1', type: 'string', description: 'x' })).toEqual({ type: 'string', description: 'x' });
  });
});
