// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Client-side preflight for the `x-aws-idp-*` class-level rules the backend
 * hard-rejects at config load.
 *
 * Before this, an invalid `x-aws-idp-instance-array` was shown with red errorText
 * reading "will be rejected on save" — and then Save worked, because Ajv only
 * checks standard JSON-Schema keywords and knew nothing about these extensions.
 * The rejection arrived later, from the server, on a configuration the user had
 * already been told was a problem.
 *
 * These mirror `validate_instance_array` in `idp_common/config/models.py`, which
 * stays authoritative; keep the two in step.
 */

import { renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { useSchemaValidation } from '../useSchemaValidation';

const validate = (cls: Record<string, unknown>) => {
  const { result } = renderHook(() => useSchemaValidation());
  return result.current.validateSchema(cls);
};

const cls = (overrides: Record<string, unknown> = {}, properties: Record<string, unknown> = { CheckNumber: { type: 'string' } }) => ({
  $id: 'Pay-Statement',
  name: 'Pay-Statement',
  type: 'object',
  'x-aws-idp-document-type': 'Pay-Statement',
  attributes: { properties, required: [] },
  ...overrides,
});

const recordsArray = { type: 'array', items: { type: 'object', properties: {} } };

describe('IDP class-level preflight validation', () => {
  it('accepts a class with neither key', () => {
    expect(validate(cls()).valid).toBe(true);
  });

  it('accepts a valid designation', () => {
    expect(validate(cls({ 'x-aws-idp-instance-array': 'records' }, { records: recordsArray })).valid).toBe(true);
  });

  it('accepts items declared as a $ref, which is what this editor emits', () => {
    const r = validate(cls({ 'x-aws-idp-instance-array': 'records' }, { records: { type: 'array', items: { $ref: '#/$defs/Rec' } } }));
    expect(r.valid).toBe(true);
  });

  it('rejects a designation naming a property that does not exist, and lists what does', () => {
    const r = validate(cls({ 'x-aws-idp-instance-array': 'nope' }, { records: recordsArray }));
    expect(r.valid).toBe(false);
    expect(r.errors.some((e) => e.message.includes('not a property of this class') && e.message.includes('records'))).toBe(true);
  });

  it('rejects a designation naming a non-array property', () => {
    const r = validate(cls({ 'x-aws-idp-instance-array': 'CheckNumber' }));
    expect(r.valid).toBe(false);
    expect(r.errors.some((e) => e.message.includes('must be an array'))).toBe(true);
  });

  it('rejects a designation whose items are not objects', () => {
    const r = validate(cls({ 'x-aws-idp-instance-array': 'tags' }, { tags: { type: 'array', items: { type: 'string' } } }));
    expect(r.valid).toBe(false);
    expect(r.errors.some((e) => e.message.includes('items are not objects'))).toBe(true);
  });

  it('rejects both keys on one class', () => {
    const r = validate(cls({ 'x-aws-idp-instance-array': 'records', 'x-aws-idp-multi-instance': true }, { records: recordsArray }));
    expect(r.valid).toBe(false);
    expect(r.errors.some((e) => e.message.includes('mutually exclusive'))).toBe(true);
  });

  it('rejects multi-instance on a class that already declares "instances"', () => {
    const r = validate(cls({ 'x-aws-idp-multi-instance': true }, { CheckNumber: { type: 'string' }, instances: { type: 'string' } }));
    expect(r.valid).toBe(false);
    expect(r.errors.some((e) => e.message.includes('would shadow'))).toBe(true);
  });

  it('honours the stringified boolean a config round-trip can produce', () => {
    const r = validate(cls({ 'x-aws-idp-instance-array': 'records', 'x-aws-idp-multi-instance': 'true' }, { records: recordsArray }));
    expect(r.valid).toBe(false);
  });

  it('accepts an ALREADY-WRAPPED schema, which legitimately carries both keys', () => {
    // Produced at runtime and never stored — but rejecting a schema the pipeline
    // itself emits would be a nasty trap for anyone who round-trips one.
    const r = validate(cls({ 'x-aws-idp-multi-instance': true, 'x-aws-idp-instance-array': 'instances' }, { instances: recordsArray }));
    expect(r.valid).toBe(true);
  });

  it('reads properties from an exported class too, not only the designer shape', () => {
    const exported = {
      $id: 'Pay-Statement',
      name: 'Pay-Statement',
      type: 'object',
      'x-aws-idp-instance-array': 'CheckNumber',
      properties: { CheckNumber: { type: 'string' } },
    };
    expect(validate(exported).valid).toBe(false);
  });
});
