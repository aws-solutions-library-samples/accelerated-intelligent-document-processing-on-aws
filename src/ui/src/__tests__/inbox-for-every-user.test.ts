// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Every signed-in user has an Inbox (its operations are ANY_GROUP in
 * scripts/api_rbac_expectations.yaml), so nothing on the client may hide it by role:
 * the notifications bell is in every user's top navigation, the Inbox is polled for
 * every user, and /inbox is routed in every shell. Home in the side navigation is
 * covered by navigation.inbox.test.tsx.
 */

import fs from 'fs';
import path from 'path';
import { describe, expect, it } from 'vitest';

const SRC = path.resolve(__dirname, '..');
const read = (...parts: string[]) => fs.readFileSync(path.join(SRC, ...parts), 'utf8');

describe('the Inbox is every user’s', () => {
  it('puts the bell in every user’s top navigation', () => {
    const nav = read('components', 'genai-idp-top-navigation', 'GenAIIDPTopNavigation.tsx');
    expect(nav).toMatch(/utilities=\{\[\s*(\/\/[^\n]*\n\s*)*inboxUtility,/);
    expect(nav).not.toMatch(/\?\s*\[inboxUtility\]/);
  });

  it('polls the Inbox for every user', () => {
    const auth = read('routes', 'AuthRoutes.tsx');
    expect(auth).toContain('<InboxProvider>');
    expect(auth).not.toMatch(/<InboxProvider enabled=/);
  });

  it('routes /inbox in both shells, with no role guard', () => {
    const auth = read('routes', 'AuthRoutes.tsx');
    const inboxRoutes = auth.match(/<Route path=\{`\$\{INBOX_PATH\}\/\*`\} element=\{<InboxRoutes \/>\} \/>/g) ?? [];
    expect(inboxRoutes).toHaveLength(2);
    expect(auth).not.toMatch(/&&\s*<Route path=\{`\$\{INBOX_PATH\}/);
  });
});
