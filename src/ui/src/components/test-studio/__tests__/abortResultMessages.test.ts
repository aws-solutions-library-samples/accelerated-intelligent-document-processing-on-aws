// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A successful abort that could not confirm every document stopped is shown as
 * such (#1609): the run is ABORTED, but the documents the response names may still
 * be processing, and a plain "Successfully aborted" banner would hide that.
 */
import { readFileSync } from 'fs';
import { resolve } from 'path';
import { describe, expect, it } from 'vitest';

import { abortSuccessMessages } from '../abortResultMessages';

describe('abortSuccessMessages', () => {
  it('reports a clean abort with no warning', () => {
    expect(
      abortSuccessMessages({ success: true, message: 'Aborted 1 test run(s)', abortedCount: 1, failedCount: 0, errors: null }),
    ).toEqual({
      success: 'Successfully aborted 1 test run',
      warning: null,
    });
  });

  it('warns, with the reasons, when documents were not confirmed stopped', () => {
    const { success, warning } = abortSuccessMessages({
      success: true,
      message: 'Aborted 1 test run(s); 3 document(s) not confirmed stopped',
      abortedCount: 1,
      failedCount: 0,
      errors: ['run-1: 3 of 3 document(s) were not confirmed stopped and may still be processing: refused'],
    });
    expect(success).toBe('Successfully aborted 1 test run');
    expect(warning).toContain('3 document(s) not confirmed stopped');
    expect(warning).toContain('run-1: 3 of 3 document(s)');
  });

  it('is what the Test Studio list shows after an abort', () => {
    const source = readFileSync(resolve(__dirname, '../TestResultsList.tsx'), 'utf8');
    const handler = source.slice(source.indexOf('const confirmAbort'), source.indexOf('return abortResult;'));
    expect(handler).toContain('abortSuccessMessages(abortResult)');
    expect(handler).toContain('setErrorMessage(warning)');
  });
});
