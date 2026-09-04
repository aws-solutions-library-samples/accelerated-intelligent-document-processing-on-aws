// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Regression tests for useSchemaDesigner.
 *
 * Critical contract: partial updates passed via updateClass / updateAttribute
 * MUST preserve unknown keys on the underlying object. This is what allows
 * users to author extension fields (e.g. `x-aws-idp-page-types`) in the YAML
 * tab and edit other class properties via the form without silently dropping
 * the YAML-only extensions. Several `x-aws-idp-*` fields don't yet have form
 * widgets — if a future refactor switches to spread-from-form-state, this
 * contract would silently break.
 */

import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { useSchemaDesigner } from '../useSchemaDesigner';

describe('useSchemaDesigner unknown-extension preservation', () => {
  it('updateClass preserves arbitrary x-aws-idp-* keys not touched by the update', () => {
    const { result } = renderHook(() => useSchemaDesigner());

    let classId = '';
    act(() => {
      const cls = result.current.addClass('TestClass');
      classId = cls.id;
    });

    // Seed a class with an unknown extension (as if loaded from YAML).
    act(() => {
      result.current.updateClass(classId, {
        'x-aws-idp-page-types': [{ name: 'AccountSummary' }],
        'x-aws-idp-future-extension': { foo: 'bar' },
      });
    });

    // Now do a partial update that touches a different field. The unknown
    // keys must remain.
    act(() => {
      result.current.updateClass(classId, { description: 'updated' });
    });

    const cls = result.current.classes.find((c) => c.id === classId);
    expect(cls).toBeDefined();
    expect(cls!.description).toBe('updated');
    expect(cls!['x-aws-idp-page-types']).toEqual([{ name: 'AccountSummary' }]);
    expect(cls!['x-aws-idp-future-extension']).toEqual({ foo: 'bar' });
  });

  it('updateAttribute preserves arbitrary x-aws-idp-* keys not touched by the update', () => {
    const { result } = renderHook(() => useSchemaDesigner());

    let classId = '';
    act(() => {
      const cls = result.current.addClass('TestClass');
      classId = cls.id;
    });

    act(() => {
      result.current.addAttribute(classId, 'AccountNumber', 'string');
    });

    // Seed unknown extensions on the attribute.
    act(() => {
      result.current.updateAttribute(classId, 'AccountNumber', {
        'x-aws-idp-source-page-types': ['AccountSummary'],
        'x-aws-idp-future-attr-extension': 42,
      });
    });

    // Touch an unrelated field.
    act(() => {
      result.current.updateAttribute(classId, 'AccountNumber', { description: 'primary id' });
    });

    const cls = result.current.classes.find((c) => c.id === classId);
    const attr = cls?.attributes.properties.AccountNumber;
    expect(attr).toBeDefined();
    expect(attr!.description).toBe('primary id');
    expect(attr!['x-aws-idp-source-page-types']).toEqual(['AccountSummary']);
    expect(attr!['x-aws-idp-future-attr-extension']).toBe(42);
  });

  it('exportSchema preserves x-aws-idp-instance-array on a document class', () => {
    // Regression: the export allow-list is hand-maintained per key, so a
    // class-level extension missing from it is SILENTLY ERASED the first time a
    // user opens and saves that class in the Document Schema editor. The key
    // would work fine via YAML/CLI and then vanish on the next UI edit.
    const { result } = renderHook(() => useSchemaDesigner());

    let classId = '';
    act(() => {
      const cls = result.current.addClass('PatientPacket');
      classId = cls.id;
    });

    act(() => {
      result.current.updateClass(classId, {
        'x-aws-idp-document-type': true,
        'x-aws-idp-instance-array': 'records',
      });
    });

    const exported = result.current.exportSchema();
    expect(exported).not.toBeNull();
    const cls = exported!.find((c) => c['x-aws-idp-instance-array'] !== undefined);
    expect(cls).toBeDefined();
    expect(cls!['x-aws-idp-instance-array']).toBe('records');
  });

  it('exportSchema preserves x-aws-idp-multi-instance on a document class', () => {
    // Same regression as the instance-array test above. There are THREE
    // hand-maintained allow-lists (two import paths + export); missing any one
    // silently erases the flag the first time a user opens and saves the class,
    // and a class that silently loses the flag reverts to extracting ONE record
    // out of N with no error anywhere.
    const { result } = renderHook(() => useSchemaDesigner());

    let classId = '';
    act(() => {
      const cls = result.current.addClass('PayStatement');
      classId = cls.id;
    });

    act(() => {
      result.current.updateClass(classId, {
        'x-aws-idp-document-type': true,
        'x-aws-idp-multi-instance': true,
      });
    });

    // Touch an unrelated field, the way the editor does on any edit.
    act(() => {
      result.current.updateClass(classId, { description: 'a pay statement' });
    });

    const exported = result.current.exportSchema();
    expect(exported).not.toBeNull();
    const cls = exported!.find((c) => c['x-aws-idp-multi-instance'] !== undefined);
    expect(cls).toBeDefined();
    expect(cls!['x-aws-idp-multi-instance']).toBe(true);
  });

  it('updateAttribute removes a key when the update value is undefined', () => {
    // Documents the existing semantics so we don't accidentally regress them
    // when changing the preservation behavior above.
    const { result } = renderHook(() => useSchemaDesigner());

    let classId = '';
    act(() => {
      const cls = result.current.addClass('TestClass');
      classId = cls.id;
    });

    act(() => {
      result.current.addAttribute(classId, 'Field', 'string');
      result.current.updateAttribute(classId, 'Field', {
        'x-aws-idp-source-page-types': ['A'],
      });
    });

    act(() => {
      result.current.updateAttribute(classId, 'Field', {
        'x-aws-idp-source-page-types': undefined,
      });
    });

    const attr = result.current.classes.find((c) => c.id === classId)?.attributes.properties.Field;
    expect(attr).toBeDefined();
    expect('x-aws-idp-source-page-types' in attr!).toBe(false);
  });
});
