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
    const when = new Date(Date.UTC(2026, 10, 20, 9, 5, 7));
    expect(newQuickStartProfileName(when, 'ab12')).toBe('quickstart-20261120-090507-ab12');
    const name = newQuickStartProfileName(when);
    expect(name).toMatch(/^quickstart-20261120-090507-[0-9a-f]{4}$/);
    expect(name).toMatch(/^[a-zA-Z0-9._-]+$/);
  });

  it('differs between two conversations started in the same second', () => {
    const when = new Date(Date.UTC(2026, 10, 20, 9, 5, 7));
    const names = new Set(Array.from({ length: 20 }, () => newQuickStartProfileName(when)));
    expect(names.size).toBeGreaterThan(1);
  });

  const target = 'quickstart-20261120-090507';

  it.each([
    ['the caller sees no profile at all, so their scope hides some', [], false],
    ['only default exists', [{ versionName: 'default', isActive: true }], true],
    [
      'a freshly deployed stack: default active beside its inactive stack-managed presets',
      [
        { versionName: 'default', isActive: true },
        { versionName: 'fake-w2', isActive: false, managed: true },
        { versionName: 'docsplit', isActive: false, managed: true },
        { versionName: 'ocr-benchmark', isActive: false, managed: true },
        { versionName: 'realkie-fcc-verified', isActive: false, managed: true },
      ],
      true,
    ],
    [
      'a stack-managed preset someone made active',
      [
        { versionName: 'default', isActive: false },
        { versionName: 'docsplit', isActive: true, managed: true },
      ],
      false,
    ],
    [
      'default active beside the presets and a profile of the stack',
      [
        { versionName: 'default', isActive: true },
        { versionName: 'docsplit', isActive: false, managed: true },
        { versionName: 'team-invoices', isActive: false },
      ],
      false,
    ],
    [
      'it is already the active profile',
      [
        { versionName: 'docsplit', isActive: false },
        { versionName: target, isActive: true },
      ],
      true,
    ],
    [
      'default is active beside a profile of the stack',
      [
        { versionName: 'default', isActive: true },
        { versionName: 'team-invoices', isActive: false },
      ],
      false,
    ],
    ['nothing is active beside a profile of the stack', [{ versionName: 'docsplit', isActive: false }], false],
    ['a stack-managed profile is active', [{ versionName: 'docsplit', isActive: true }], false],
    ['a user profile is active', [{ versionName: 'team-invoices', isActive: true }], false],
  ])('is made active without asking only when the stack has no profile of its own (%s)', (_case, versions, activates) => {
    expect(activatesWithoutAsking(versions, target)).toBe(activates);
  });

  it('is decided when Discovery finishes, from a refreshed list that loaded without error', () => {
    const layout = readFileSync(join(__dirname, '..', 'AgentChatLayout.tsx'), 'utf-8');
    expect(layout).toMatch(/const version = discoveryTargetRef\.current;\s*void fetchVersions\(\);/);
    expect(layout).toMatch(/latest\.loaded && !latest\.error && activatesWithoutAsking\(latest\.versions, result\.configVersion\)/);
  });

  it('tells the agent it is active only once the activation has succeeded', () => {
    const layout = readFileSync(join(__dirname, '..', 'AgentChatLayout.tsx'), 'utf-8');
    expect(layout).toMatch(/setActiveVersion\(result\.configVersion\)\s*\.then\(\(\) => send\(true\)\)/);
    expect(layout).toMatch(/\.catch\(\(e\) => \{[^}]*send\(false\);/);
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
