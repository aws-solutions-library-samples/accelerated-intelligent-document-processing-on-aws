// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Inbox code must not use the name `Notification`: a type or component by that
 * name shadows the DOM `Notification`, and `useNotifications` / `use-notifications`
 * already mean the app's flash messages. The one sanctioned use is InboxLayout
 * wiring that existing flashbar, allowed line by line rather than by skipping the
 * file.
 */

import fs from 'fs';
import path from 'path';
import { describe, expect, it } from 'vitest';

const SRC = path.resolve(__dirname, '..');

const INBOX_DIR = path.join(SRC, 'components', 'inbox');
const SINGLE_FILES = [
  path.join(SRC, 'contexts', 'inbox.tsx'),
  path.join(SRC, 'hooks', 'use-inbox.ts'),
  path.join(SRC, 'routes', 'InboxRoutes.tsx'),
];

const FORBIDDEN = [/\bNotification\b/, /useNotifications/, /use-notifications/];

// The InboxLayout lines that wire the app's existing flashbar.
const FLASHBAR_LINES = new Set([
  "import useNotifications from '../../hooks/use-notifications';",
  "import { mapNotificationsToFlashbar } from '../common/notification-flashbar';",
  'const flashItems = mapNotificationsToFlashbar(useNotifications());',
]);
const INBOX_LAYOUT = path.join(INBOX_DIR, 'InboxLayout.tsx');

const walk = (dir: string): string[] =>
  fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    return entry.isDirectory() ? walk(full) : [full];
  });

const inboxFiles = (): string[] => [...walk(INBOX_DIR), ...SINGLE_FILES];

// This guard lives outside the scanned trees, so its own patterns are not scanned.
const scanned = inboxFiles;

describe('Inbox naming', () => {
  it('scans the Inbox sources', () => {
    const files = scanned();
    expect(files).toContain(INBOX_LAYOUT);
    expect(files.length).toBeGreaterThan(SINGLE_FILES.length);
    SINGLE_FILES.forEach((f) => expect(fs.existsSync(f)).toBe(true));
  });

  it('uses no Notification identifiers outside the flashbar wiring', () => {
    const findings: string[] = [];
    scanned().forEach((file) => {
      fs.readFileSync(file, 'utf8')
        .split('\n')
        .forEach((line, index) => {
          if (!FORBIDDEN.some((re) => re.test(line))) return;
          if (file === INBOX_LAYOUT && FLASHBAR_LINES.has(line.trim())) return;
          findings.push(`${path.relative(SRC, file)}:${index + 1}: ${line.trim()}`);
        });
    });
    expect(findings).toEqual([]);
  });

  it('still contains each allowed flashbar line, so the allowance is not dead', () => {
    const lines = new Set(
      fs
        .readFileSync(INBOX_LAYOUT, 'utf8')
        .split('\n')
        .map((l) => l.trim()),
    );
    FLASHBAR_LINES.forEach((allowed) => expect(lines.has(allowed)).toBe(true));
  });

  it('adds no file whose name mentions notification', () => {
    const named = inboxFiles().filter((f) => /notification/i.test(path.basename(f)));
    expect(named).toEqual([]);
  });
});
