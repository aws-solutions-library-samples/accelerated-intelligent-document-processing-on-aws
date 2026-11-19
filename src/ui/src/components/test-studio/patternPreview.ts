// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * What a file pattern will actually add to a test set, for the Check Files preview.
 *
 * `listBucketFiles` returns every key the pattern matches, but the copier that adds
 * them (`src/lambda/test_set_file_copier/index.py`) leaves two kinds out: a `.zip`
 * from either bucket (`_split_documents`, because one copied into a set's
 * `input/` folder starts the zip extractor), and a file the set already holds
 * (`_split_pattern_members`). The preview counts what is left, so the number shown
 * before Add Documents is the number the copy can add.
 */

import { defineMessages, type IntlShape } from 'react-intl';

/** The suffix the copier's `ZIP_EXTRACTOR_SUFFIX` names, compared case-insensitively there too. */
export const ARCHIVE_SUFFIX = '.zip';

export interface PatternPreview {
  /** Matches the copy would add. */
  documents: string[];
  /** `.zip` matches, which are left out. */
  archives: string[];
  /** Matches the set already holds, which are left as they are. */
  alreadyInSet: string[];
}

/**
 * The name a matched key lands under in the set's `input/` folder, as the copier's
 * `_set_member_name` derives it: a Test Set bucket key `<set>/input/<name>` lands as
 * `<name>`, and every other key lands as itself.
 */
export const setMemberName = (bucketType: string, key: string): string => {
  if (bucketType === 'testset') {
    const parts = key.split('/');
    if (parts.length >= 3 && parts[1] === 'input') return parts.slice(2).join('/');
  }
  return key;
};

/** The pattern that lists a set's own `input/` folder in the Test Set bucket. */
export const setInputPattern = (testSetId: string): string => `${testSetId}/input/**`;

/**
 * Member names from a listing of `setInputPattern(testSetId)`: each key with the
 * `<set>/input/` prefix taken off.
 */
export const memberNames = (testSetId: string, keys: string[]): Set<string> => {
  const prefix = `${testSetId}/input/`;
  return new Set(keys.filter((key) => key.startsWith(prefix)).map((key) => key.slice(prefix.length)));
};

export const splitPatternMatches = (bucketType: string, matches: string[], members: Set<string> = new Set()): PatternPreview => {
  const documents: string[] = [];
  const archives: string[] = [];
  const alreadyInSet: string[] = [];
  matches.forEach((key) => {
    if (key.toLowerCase().endsWith(ARCHIVE_SUFFIX)) {
      archives.push(key);
    } else if (members.has(setMemberName(bucketType, key))) {
      alreadyInSet.push(key);
    } else {
      documents.push(key);
    }
  });
  return { documents, archives, alreadyInSet };
};

/** What Check Files shows; `formatLeftOut` joins the two left-out counts. */
export const patternPreviewMessages = defineMessages<{
  filesToAdd: { readonly count: number };
  alreadyInSet: { readonly count: number };
  archivesLeftOut: { readonly count: number };
  leftOutNote: { readonly leftOut: string };
  noneToAdd: { readonly count: number; readonly leftOut: string };
}>({
  filesToAdd: {
    id: 'testStudio.patternPreview.filesToAdd',
    defaultMessage: '{count, plural, one {# file} other {# files}} to add',
    description: 'Test Studio Check Files result: how many files matching the pattern the add will copy into the set.',
  },
  alreadyInSet: {
    id: 'testStudio.patternPreview.alreadyInSet',
    defaultMessage: '{count, number} already in the set',
    description: 'Part of the Check Files result: matching files the set already holds, which the add leaves as they are.',
  },
  archivesLeftOut: {
    id: 'testStudio.patternPreview.archivesLeftOut',
    defaultMessage: '{count, plural, one {# .zip archive} other {# .zip archives}} left out',
    description: 'Part of the Check Files result: matching .zip files, which the add does not copy.',
  },
  leftOutNote: {
    id: 'testStudio.patternPreview.leftOutNote',
    defaultMessage: '({leftOut})',
    description: 'Shown after the Check Files count. {leftOut} lists the matching files the add leaves out, e.g. "1 already in the set".',
  },
  noneToAdd: {
    id: 'testStudio.patternPreview.noneToAdd',
    defaultMessage: 'None of the {count, plural, one {# matching file} other {# matching files}} would be added ({leftOut}).',
    description:
      'Warning when Check Files matched files but the add would copy none of them. {leftOut} says why, e.g. "1 already in the set".',
  },
});

/** The left-out counts as one locale-joined phrase, or '' when nothing was left out. */
export const formatLeftOut = (intl: IntlShape, { archives, alreadyInSet }: PatternPreview): string => {
  const parts: string[] = [];
  if (alreadyInSet.length) parts.push(intl.formatMessage(patternPreviewMessages.alreadyInSet, { count: alreadyInSet.length }));
  if (archives.length) parts.push(intl.formatMessage(patternPreviewMessages.archivesLeftOut, { count: archives.length }));
  return intl.formatList(parts, { type: 'conjunction' });
};
