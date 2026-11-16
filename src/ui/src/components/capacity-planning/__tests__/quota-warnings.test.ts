// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it } from 'vitest';
import { QUOTA_NOT_READ_PREFIX, quotaNotReadWarnings } from '../quota-warnings';

describe('quotaNotReadWarnings', () => {
  it('keeps only the unreadable-quota recommendations, in order', () => {
    const first = `${QUOTA_NOT_READ_PREFIX} TPM quota for model a (quota code L-A) could not be read (x).`;
    const second = `${QUOTA_NOT_READ_PREFIX} RPM quota for model b (quota code L-B) could not be read (y).`;
    expect(quotaNotReadWarnings([first, '⚠️ High load detected', null, '📈 2 quota increases needed', second])).toEqual([first, second]);
  });

  it('does not treat other warning recommendations as unreadable quotas', () => {
    expect(quotaNotReadWarnings(['⚠️ Quota increases needed for: a'])).toEqual([]);
  });

  it('accepts an absent list', () => {
    expect(quotaNotReadWarnings(undefined)).toEqual([]);
    expect(quotaNotReadWarnings(null)).toEqual([]);
  });
});
