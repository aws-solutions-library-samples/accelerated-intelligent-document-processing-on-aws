// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it } from 'vitest';

import { INBOX_PATH, documentDetailHref, inboxHref, isInternalRoute } from '../constants';

describe('inboxHref', () => {
  it('links to the inbox, optionally with a tab', () => {
    expect(INBOX_PATH).toBe('/inbox');
    expect(inboxHref()).toBe('#/inbox');
    expect(inboxHref('messages')).toBe('#/inbox?tab=messages');
    expect(inboxHref('preferences')).toBe('#/inbox?tab=preferences');
  });
});

describe('documentDetailHref', () => {
  it('encodes the whole object key like the Python document_detail_link', () => {
    expect(documentDetailHref('a/b c!.pdf')).toBe('#/documents/a%2Fb%20c!.pdf');
  });
});

describe('isInternalRoute', () => {
  // Same matrix as TestLinks in lib/idp_common_pkg/tests/unit/notifications/test_models.py.
  it.each(['https://x', '//x', '/\\x', 'javascript:alert(1)', '/a b', '/a\x01', '/x?u=https://y'])('rejects %j', (link) => {
    expect(isInternalRoute(link)).toBe(false);
  });

  it.each(['/documents/a%2Fb', '/inbox', '/x?u=https%3A%2F%2Fy'])('accepts %j', (link) => {
    expect(isInternalRoute(link)).toBe(true);
  });

  it('rejects empty, missing and oversized links', () => {
    expect(isInternalRoute(undefined)).toBe(false);
    expect(isInternalRoute(null)).toBe(false);
    expect(isInternalRoute('')).toBe(false);
    expect(isInternalRoute(`/${'a'.repeat(1024)}`)).toBe(false);
    expect(isInternalRoute(`/${'a'.repeat(1023)}`)).toBe(true);
  });

  it('rejects a mixed-case scheme separator', () => {
    expect(isInternalRoute('/x?u=HTTPS://y')).toBe(false);
  });
});
