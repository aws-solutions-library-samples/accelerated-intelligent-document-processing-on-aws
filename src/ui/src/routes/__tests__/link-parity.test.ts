// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The UI's link builders equal idp_common's, byte for byte, over one shared fixture
 * (`lib/idp_common_pkg/tests/resources/link_parity.json`, also read by
 * `tests/unit/notifications/test_link_parity.py`). Inbox messages store the link the
 * backend built, so the routes must decode exactly what it encodes.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { documentDetailHref, documentEditHref, testSetItemPath } from '../constants';

interface LinkCase {
  objectKey: string;
  testSetId: string;
  documentDetail: string;
  documentEdit: string;
  testSetItem: string;
}

const FIXTURE = join(__dirname, '..', '..', '..', '..', '..', 'lib', 'idp_common_pkg', 'tests', 'resources', 'link_parity.json');
const { cases } = JSON.parse(readFileSync(FIXTURE, 'utf-8')) as { cases: LinkCase[] };

describe('the link builders match idp_common', () => {
  it('reads the shared fixture', () => {
    expect(cases.length).toBeGreaterThan(0);
  });

  it.each(cases.map((c) => [c.objectKey, c] as const))('%s', (_key, c) => {
    expect(documentDetailHref(c.objectKey).slice(1)).toBe(c.documentDetail);
    expect(documentEditHref(c.objectKey).slice(1)).toBe(c.documentEdit);
    expect(testSetItemPath(c.testSetId, c.objectKey)).toBe(c.testSetItem);
  });

  it('adds only the query keys given, in order', () => {
    expect(documentEditHref('a/b.pdf', { field: 'x.y', section: '2', view: 'source' })).toBe(
      '#/documents/a%2Fb.pdf/edit?section=2&field=x.y&view=source',
    );
    expect(documentEditHref('a/b.pdf', { field: 'Line Items[0]' })).toBe('#/documents/a%2Fb.pdf/edit?field=Line%20Items%5B0%5D');
  });
});
