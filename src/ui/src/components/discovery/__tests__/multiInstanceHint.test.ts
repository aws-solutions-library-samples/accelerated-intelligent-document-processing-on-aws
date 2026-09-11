// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it } from 'vitest';

import { applyMultiInstance, parseMultiInstanceHint } from '../multiInstanceHint';

describe('parseMultiInstanceHint', () => {
  it('accepts the backend shape and rejects anything that is not a real suggestion', () => {
    const ok = parseMultiInstanceHint(JSON.stringify({ instance_count: 3, class_name: 'Paystub', message: 'm' }));
    expect(ok?.instance_count).toBe(3);
    expect(parseMultiInstanceHint(null)).toBeNull();
    expect(parseMultiInstanceHint('not json')).toBeNull();
    expect(parseMultiInstanceHint(JSON.stringify({ instance_count: 1, class_name: 'Paystub' }))).toBeNull();
    expect(parseMultiInstanceHint(JSON.stringify({ instance_count: 2 }))).toBeNull();
  });
});

describe('applyMultiInstance', () => {
  const classes: Record<string, unknown>[] = [
    { $id: 'Invoice', properties: {} },
    { $id: 'Paystub', 'x-aws-idp-document-type': 'Paystub', 'x-aws-idp-instance-array': 'stubs', properties: {} },
  ];

  it('flags only the named class and clears a designated instance array', () => {
    const r = applyMultiInstance(classes, 'Paystub');
    expect(r.found).toBe(true);
    expect(r.changed).toBe(true);
    expect(r.classes[0]).toBe(classes[0]);
    expect(r.classes[1]['x-aws-idp-multi-instance']).toBe(true);
    expect('x-aws-idp-instance-array' in r.classes[1]).toBe(false);
    // input untouched
    expect(classes[1]['x-aws-idp-multi-instance']).toBeUndefined();
  });

  it('reports not found and already enabled without changing anything', () => {
    expect(applyMultiInstance(classes, 'W2').found).toBe(false);
    const already = [{ $id: 'Paystub', 'x-aws-idp-multi-instance': true }];
    const r = applyMultiInstance(already, 'Paystub');
    expect(r.found).toBe(true);
    expect(r.changed).toBe(false);
  });
});
