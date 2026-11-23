// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Check Files preview counts what the copier would add, so its rules are the
 * copier's: a `.zip` from either bucket is left out, and so is a file the set holds.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { createIntl } from 'react-intl';
import { ARCHIVE_SUFFIX, formatLeftOut, memberNames, setInputPattern, setMemberName, splitPatternMatches } from '../patternPreview';

const ROOT = join(__dirname, '..', '..', '..', '..', '..', '..');
const COPIER = readFileSync(join(ROOT, 'src', 'lambda', 'test_set_file_copier', 'index.py'), 'utf-8');

describe('splitPatternMatches', () => {
  it('leaves an Input-bucket .zip out, in any case', () => {
    const split = splitPatternMatches('input', ['a.pdf', 'b.ZIP', 'c/d.zip', 'e.zip.pdf']);
    expect(split.documents).toEqual(['a.pdf', 'e.zip.pdf']);
    expect(split.archives).toEqual(['b.ZIP', 'c/d.zip']);
    expect(split.alreadyInSet).toEqual([]);
  });

  it('leaves a .zip from the Test Set bucket out too, as the copier does', () => {
    const split = splitPatternMatches('testset', ['other/input/a.zip', 'other/input/b.pdf']);
    expect(split.documents).toEqual(['other/input/b.pdf']);
    expect(split.archives).toEqual(['other/input/a.zip']);
  });

  it('leaves out a match the set already holds, by the name it would land under', () => {
    const members = new Set(['a.pdf', 'sub/b.pdf']);
    expect(splitPatternMatches('input', ['a.pdf', 'sub/b.pdf', 'c.pdf'], members)).toEqual({
      documents: ['c.pdf'],
      archives: [],
      alreadyInSet: ['a.pdf', 'sub/b.pdf'],
    });
    expect(splitPatternMatches('testset', ['other/input/a.pdf', 'other/input/c.pdf', 'other/input/sub/b.pdf'], members)).toEqual({
      documents: ['other/input/c.pdf'],
      archives: [],
      alreadyInSet: ['other/input/a.pdf', 'other/input/sub/b.pdf'],
    });
  });

  it('counts an Input-bucket .zip the set also holds as an archive, as the copier filters archives first', () => {
    const split = splitPatternMatches('input', ['x.zip'], new Set(['x.zip']));
    expect(split.archives).toEqual(['x.zip']);
    expect(split.alreadyInSet).toEqual([]);
  });
});

describe('member names', () => {
  it('lands a Test Set bucket key under the path after <set>/input/, and any other key as itself', () => {
    expect(setMemberName('testset', 'other/input/sub/a.pdf')).toBe('sub/a.pdf');
    expect(setMemberName('testset', 'other/a.pdf')).toBe('other/a.pdf');
    expect(setMemberName('input', 'x/input/a.pdf')).toBe('x/input/a.pdf');
  });

  it("reads the set's own input/ listing with the prefix taken off", () => {
    expect(setInputPattern('invoices')).toBe('invoices/input/**');
    expect([...memberNames('invoices', ['invoices/input/a.pdf', 'invoices/input/sub/b.pdf', 'invoices/baseline/a.pdf'])]).toEqual([
      'a.pdf',
      'sub/b.pdf',
    ]);
  });
});

describe('formatLeftOut', () => {
  const intl = createIntl({ locale: 'en', messages: {} });
  it('names what was left out the way the copier reports it, and nothing when nothing was', () => {
    expect(formatLeftOut(intl, { documents: ['a'], archives: [], alreadyInSet: [] })).toBe('');
    expect(formatLeftOut(intl, { documents: [], archives: ['z.zip'], alreadyInSet: ['a', 'b'] })).toBe(
      '2 already in the set and 1 .zip archive left out',
    );
    expect(formatLeftOut(intl, { documents: [], archives: ['y.zip', 'z.zip'], alreadyInSet: [] })).toBe('2 .zip archives left out');
  });
});

describe('the copier these rules come from', () => {
  it('filters on the same suffix, case-insensitively, for either bucket', () => {
    expect(COPIER).toContain(`ZIP_EXTRACTOR_SUFFIX = "${ARCHIVE_SUFFIX}"`);
    expect(COPIER).toContain('if file_key.lower().endswith(ZIP_EXTRACTOR_SUFFIX):');
    // Unconditional: directly after the empty-match check, not under a bucket test.
    expect(COPIER).toMatch(
      /raise ValueError\(f"No files found matching pattern: \{file_pattern\}"\)\n(?:\s+#[^\n]*\n)*\s+matching_files, not_documents = _split_documents\(matching_files\)/,
    );
    expect(COPIER).not.toMatch(/if bucket_type == "input":\n\s+matching_files, not_documents = _split_documents/);
  });

  it('names members by the path after <set>/input/ for the Test Set bucket', () => {
    expect(COPIER).toMatch(/if len\(path_parts\) >= 3 and path_parts\[1\] == "input":\n\s+return "\/"\.join\(path_parts\[2:\]\)/);
    expect(COPIER).toContain('prefix = f"{test_set_id}/input/"');
  });
});

describe('the create wizard', () => {
  // Asserted at source level, as the sibling wizard tests are.
  const WIZARD = readFileSync(join(__dirname, '..', 'CreateTestSetWizard.tsx'), 'utf-8');

  it('counts the matches a new set would get, not every match', () => {
    const check = WIZARD.slice(WIZARD.indexOf('const handleCheckFiles = async'), WIZARD.indexOf('const nameIsFree = async'));
    expect(check).toMatch(/const split = splitPatternMatches\(bucket\.value \?\? '', matched\);/);
    expect(check).toMatch(/setFileCount\(split\.documents\.length\);/);
    expect(check).not.toMatch(/setFileCount\(matched\.length\)/);
  });
});
