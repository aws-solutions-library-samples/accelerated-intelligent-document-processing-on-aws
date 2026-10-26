// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Pages panel is given the document's own configuration profile.
 *
 * PagesPanel decides BDA mode from that profile's `use_bda`, and without it reads every
 * document as pipeline mode — so page edits would be offered on a BDA-mode document
 * again, with every PagesPanel test still green, since those pass the config directly.
 * Asserted against the source, as DocumentPanel.reviewButton.test.ts does: rendering
 * DocumentPanel means mocking most of the application.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'DocumentPanel.tsx'), 'utf-8');

describe('DocumentPanel wiring of the Pages panel', () => {
  it("passes the document's configuration profile to PagesPanel", () => {
    const at = SOURCE.indexOf('<PagesPanel');
    expect(at).toBeGreaterThan(-1);
    const element = SOURCE.slice(at, SOURCE.indexOf('/>', at));
    expect(element).toMatch(/^\s*mergedConfig: documentVersionConfig,$/m);
  });

  it('passes the same profile the Sections panel receives', () => {
    const at = SOURCE.indexOf('<SectionsPanel');
    const element = SOURCE.slice(at, SOURCE.indexOf('/>', at));
    expect(element).toMatch(/^\s*mergedConfig: documentVersionConfig,$/m);
  });
});
