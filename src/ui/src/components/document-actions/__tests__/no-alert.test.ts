// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The document page reports results through the shared notifications. What has to hold:
 * no `alert(` and no `console.log` in the page, its panels, the page text editor or the
 * document actions, whose results a user would otherwise see only as a browser popup, or
 * not at all.
 */

import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const COMPONENTS = join(__dirname, '..', '..');
const ACTIONS = join(COMPONENTS, 'document-actions');

const FILES = [
  join(COMPONENTS, 'document-panel', 'DocumentPanel.tsx'),
  join(COMPONENTS, 'document-details', 'DocumentDetails.tsx'),
  join(COMPONENTS, 'sections-panel', 'SectionsPanel.tsx'),
  join(COMPONENTS, 'pages-panel', 'PagesPanel.tsx'),
  join(COMPONENTS, 'pages-panel', 'PageTextEditorModal.tsx'),
  ...readdirSync(ACTIONS)
    .filter((f) => /\.tsx?$/.test(f))
    .map((f) => join(ACTIONS, f)),
];

describe('the document page reports results through the notifications', () => {
  it.each(FILES.map((f) => [f.slice(COMPONENTS.length + 1), f]))('%s has no alert( or console.log', (_name, file) => {
    const source = readFileSync(file, 'utf-8');
    expect(source).not.toMatch(/\balert\(/);
    expect(source).not.toMatch(/console\.log/);
  });
});
