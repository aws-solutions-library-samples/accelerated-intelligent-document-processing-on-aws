// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it } from 'vitest';
import { isReplaceOnActive } from '../replaceGuard';

const versions = [
  { versionName: 'production', isActive: true },
  { versionName: 'staging', isActive: false },
];

describe('isReplaceOnActive', () => {
  it('flags replace on the active profile', () => {
    expect(isReplaceOnActive('replace', 'production', versions)).toBe(true);
  });

  it('allows replace on another profile', () => {
    expect(isReplaceOnActive('replace', 'staging', versions)).toBe(false);
  });

  it('allows augment on the active profile', () => {
    expect(isReplaceOnActive('augment', 'production', versions)).toBe(false);
  });

  it('allows a profile not in the list (a new one)', () => {
    expect(isReplaceOnActive('replace', 'brand-new', versions)).toBe(false);
  });

  it('allows when no profile is selected', () => {
    expect(isReplaceOnActive('replace', null, versions)).toBe(false);
  });
});
