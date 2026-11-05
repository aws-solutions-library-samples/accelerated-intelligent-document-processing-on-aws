// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * distinguishingNames, the short display name of each object key in a list: the
 * fewest trailing path segments that tell it apart from every other key.
 */

import { describe, expect, it } from 'vitest';

import { distinguishingNames } from '../distinguishing-names';

describe('distinguishingNames', () => {
  it('shows only the file name when file names are unique', () => {
    const names = distinguishingNames(['in/a/one.pdf', 'in/b/two.pdf']);
    expect(names.get('in/a/one.pdf')).toBe('one.pdf');
    expect(names.get('in/b/two.pdf')).toBe('two.pdf');
  });

  it('adds the folder when the same file name appears in different folders', () => {
    const names = distinguishingNames(['batch-1/lending_package.pdf', 'batch-2/lending_package.pdf', 'other.pdf']);
    expect(names.get('batch-1/lending_package.pdf')).toBe('batch-1/lending_package.pdf');
    expect(names.get('batch-2/lending_package.pdf')).toBe('batch-2/lending_package.pdf');
    expect(names.get('other.pdf')).toBe('other.pdf');
  });

  it('uses the shortest suffix that is unique, even when prefixes are shared', () => {
    const names = distinguishingNames(['root/x/docs/f.pdf', 'root/y/docs/f.pdf', 'root/x/misc/f.pdf']);
    expect(names.get('root/x/docs/f.pdf')).toBe('x/docs/f.pdf');
    expect(names.get('root/y/docs/f.pdf')).toBe('y/docs/f.pdf');
    expect(names.get('root/x/misc/f.pdf')).toBe('misc/f.pdf');
    expect(new Set(names.values()).size).toBe(3);
  });

  it('shows the full key when it is a trailing suffix of a longer key', () => {
    const names = distinguishingNames(['docs/f.pdf', 'a/docs/f.pdf']);
    expect(names.get('docs/f.pdf')).toBe('docs/f.pdf');
    expect(names.get('a/docs/f.pdf')).toBe('a/docs/f.pdf');
  });

  it('maps a duplicated key to the full key', () => {
    const names = distinguishingNames(['in/a/one.pdf', 'in/a/one.pdf', 'in/b/two.pdf']);
    expect(names.get('in/a/one.pdf')).toBe('in/a/one.pdf');
    expect(names.get('in/b/two.pdf')).toBe('two.pdf');
    expect(names.size).toBe(2);
  });

  it('returns an empty map for no keys', () => {
    expect(distinguishingNames([]).size).toBe(0);
  });
});
