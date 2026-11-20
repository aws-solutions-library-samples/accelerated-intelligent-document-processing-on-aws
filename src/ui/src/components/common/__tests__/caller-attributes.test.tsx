// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Caller-supplied document attributes on the document detail page (#1851).
 *
 * The values are untrusted caller input, so what has to hold is that they reach
 * the page as TEXT: a value carrying markup renders as its literal characters and
 * creates no element. And a document without attributes renders no field at all,
 * so its detail grid is unchanged.
 */

import React from 'react';
import { describe, expect, it } from 'vitest';

import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import { parseCallerAttributes } from '../caller-attributes';
import mapDocumentsAttributes from '../map-document-attributes';
import { CallerAttributesField } from '../../document-panel/DocumentPanel';

describe('parseCallerAttributes', () => {
  it('sorts by key and renders every accepted value type as text', () => {
    expect(parseCallerAttributes({ b: 2.5, a: 'x', c: true, d: ['p', 1, false] })).toEqual([
      { key: 'a', value: 'x' },
      { key: 'b', value: '2.5' },
      { key: 'c', value: 'true' },
      { key: 'd', value: 'p, 1, false' },
    ]);
  });

  it('accepts the same map as a JSON string', () => {
    expect(parseCallerAttributes('{"account_id":"ACME-0042"}')).toEqual([{ key: 'account_id', value: 'ACME-0042' }]);
  });

  it.each([undefined, null, '', 'not json', '[1]', 3, []])('yields no rows for %j', (raw) => {
    expect(parseCallerAttributes(raw)).toEqual([]);
  });
});

describe('mapDocumentsAttributes', () => {
  it('maps DocumentAttributes to callerAttributes', () => {
    const [row] = mapDocumentsAttributes([{ ObjectKey: 'd.pdf', DocumentAttributes: { account_id: 'A' } }]);
    expect(row.callerAttributes).toEqual([{ key: 'account_id', value: 'A' }]);
  });

  it('maps a document without attributes to an empty list', () => {
    const [row] = mapDocumentsAttributes([{ ObjectKey: 'd.pdf' }]);
    expect(row.callerAttributes).toEqual([]);
  });
});

describe('CallerAttributesField', () => {
  it('renders markup in a value as literal text, creating no element', () => {
    const hostile = '<img src=x onerror="alert(1)"><b>bold</b>';
    const { container } = renderWithIntl(
      <CallerAttributesField entries={parseCallerAttributes({ note: hostile, account_id: 'ACME-0042' })} />,
    );
    expect(container.textContent).toContain(`note: ${hostile}`);
    expect(container.textContent).toContain('account_id: ACME-0042');
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
  });

  it('renders nothing for a document without attributes', () => {
    const { container } = renderWithIntl(<CallerAttributesField entries={[]} />);
    expect(container.textContent).toBe('');
    const { container: absent } = renderWithIntl(<CallerAttributesField />);
    expect(absent.textContent).toBe('');
  });
});
