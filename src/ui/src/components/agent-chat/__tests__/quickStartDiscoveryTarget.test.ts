// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Where Discovery saves the documents a Quick Start conversation attaches.
 *
 * It used to merge them into whatever profile was active, so on a shared stack it
 * wrote one user's class into a stack-managed profile or into another user's
 * profile. A conversation now saves into a profile of its own, and makes it active by
 * itself only on a stack with no configuration of its own yet.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import { activatesWithoutAsking, newQuickStartProfileName } from '../quickStartDiscoveryTarget';

describe('the profile a Quick Start conversation saves into', () => {
  it('is a new, valid profile name taken from the time', () => {
    const name = newQuickStartProfileName(new Date(Date.UTC(2026, 10, 20, 9, 5, 7)));
    expect(name).toBe('quickstart-20261120-090507');
    expect(name).toMatch(/^[a-zA-Z0-9._-]+$/);
  });

  const target = 'quickstart-20261120-090507';

  it.each([
    ['nothing is active', [{ versionName: 'docsplit', isActive: false }], true],
    ['only default is active', [{ versionName: 'default', isActive: true }], true],
    ['it is already the active profile', [{ versionName: target, isActive: true }], true],
    ['a stack-managed profile is active', [{ versionName: 'docsplit', isActive: true }], false],
    ['a user profile is active', [{ versionName: 'team-invoices', isActive: true }], false],
  ])('is made active without asking only when the stack has no configuration of its own (%s)', (_case, versions, activates) => {
    expect(activatesWithoutAsking(versions, target)).toBe(activates);
  });

  it('is what the chat layout uploads to, not the active profile', () => {
    const layout = readFileSync(join(__dirname, '..', 'AgentChatLayout.tsx'), 'utf-8');
    expect(layout).toMatch(/discoveryTargetRef\.current = newQuickStartProfileName\(/);
    expect(layout).toMatch(/const version = discoveryTargetRef\.current;/);
    expect(layout).not.toMatch(/active\.versionName !== 'default'/);
    expect(layout).not.toMatch(/setActiveVersion\('quickstart'\)/);
  });

  it('is new again for each conversation: after Clear chat and after loading another one', () => {
    const layout = readFileSync(join(__dirname, '..', 'AgentChatLayout.tsx'), 'utf-8');
    expect(layout).toMatch(/clearChat\(\);\s*discoveryTargetRef\.current = null;/);
    expect(layout).toMatch(/setIsLoadingSession\(true\);\s*discoveryTargetRef\.current = null;/);
  });
});
