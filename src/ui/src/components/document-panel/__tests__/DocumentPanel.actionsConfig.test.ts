// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The document page's actions are given the document's own configuration profile.
 *
 * useDocumentActions decides BDA mode from that profile's `use_bda`, and without it reads
 * every document as pipeline mode — so Edit Page Class and the re-extraction actions would
 * be offered on a BDA-mode document again, with every document-bar test still green,
 * since those mock the configuration directly. Asserted against the source: rendering
 * DocumentPanel with two configurations means mocking most of the application.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'DocumentPanel.tsx'), 'utf-8');

const callOf = (name: string): string => {
  const at = SOURCE.indexOf(`${name}({`);
  expect(at, name).toBeGreaterThan(-1);
  return SOURCE.slice(at, SOURCE.indexOf('});', at));
};

describe('DocumentPanel wiring of the document actions', () => {
  it("passes the document's configuration profile to useDocumentActions", () => {
    expect(callOf('useDocumentActions')).toMatch(/^\s*mergedConfig: documentVersionConfig,$/m);
  });

  it('and the loading state of that same profile', () => {
    expect(SOURCE).toMatch(/const documentVersionConfig = needsSeparateVersionFetch \? separateVersionConfig : mergedConfig;/);
    expect(SOURCE).toMatch(/const configLoading = !!\(needsSeparateVersionFetch \? versionConfigLoading : defaultConfigLoading\);/);
    expect(callOf('useDocumentActions')).toMatch(/^\s*configLoading,$/m);
  });
});
