// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A 403 must be told apart from a failure, and said differently.
 *
 * `rest-client.ts` throws the dispatcher's JSON body verbatim and discards the HTTP
 * status, so `errorType: "Unauthorized"` is the only reliable signal that the API
 * refused the call on authorization grounds. Before this, the document list
 * interpolated the server's own words — `Failed to list documents (Unauthorized):
 * Unauthorized: listDocuments requires one of [...]` — which names Cognito groups
 * the reader cannot grant themselves, and four file viewers offered "Please try
 * again", which for a 403 is advice that can never work.
 */

import { describe, expect, it } from 'vitest';

import { describeApiError, FILE_ACCESS_DENIED_MESSAGE, isAuthorizationError } from '../graphql-error';

/** What the dispatcher's 403 body looks like once rest-client.ts has thrown it. */
const dispatcherDenial = (field: string) => ({
  errors: [
    {
      message: `Unauthorized: ${field} requires one of ['Admin', 'Annotator', 'Author', 'Reviewer', 'Viewer']`,
      errorType: 'Unauthorized',
    },
  ],
});

describe('isAuthorizationError', () => {
  it('recognises the dispatcher denial envelope', () => {
    expect(isAuthorizationError(dispatcherDenial('listDocuments'))).toBe(true);
  });

  it('recognises a resolver that raises a bare Unauthorized-prefixed message', () => {
    // Some resolvers still rely on the message prefix rather than an errorType,
    // which the dispatcher maps to 403 by text.
    expect(isAuthorizationError({ errors: [{ message: 'Unauthorized: out of scope' }] })).toBe(true);
  });

  it('does not treat an ordinary failure as an authorization failure', () => {
    expect(isAuthorizationError({ errors: [{ message: 'Internal error', errorType: 'HttpError' }] })).toBe(false);
    expect(isAuthorizationError(new Error('network down'))).toBe(false);
    expect(isAuthorizationError(undefined)).toBe(false);
  });

  it('does not read a server-side S3 denial as the caller lacking a role', () => {
    // get_file_contents_resolver wraps an unexpected ClientError as
    // `Error accessing S3: <message>`, and S3's message when the LAMBDA's role, the
    // bucket policy or the KMS key denies is literally "Access Denied". That
    // resolver has no @api_resolver wrapper, so it arrives as HTTP 500 /
    // errorType "InternalError" with the message intact. Telling the user to ask an
    // administrator for a role would be confidently wrong about a server defect no
    // role can fix.
    expect(
      isAuthorizationError({
        errors: [{ message: 'Error accessing S3: Access Denied', errorType: 'InternalError' }],
      }),
    ).toBe(false);
  });

  it('still recognises the in-band config-scope denial, which has no errorType', () => {
    // The configuration and sync resolvers report an out-of-scope configuration
    // version as HTTP 200 with a message beginning "Access denied:". That wording
    // is the only thing identifying it, so the substring arm must stay live where
    // no error type is stated.
    expect(isAuthorizationError({ errors: [{ message: 'Access denied: version out of scope' }] })).toBe(true);
  });

  it('does not match a message that merely mentions the word', () => {
    // The prefix test is deliberate: a document whose CONTENT contains the word
    // must not turn a 500 into a permissions message.
    expect(isAuthorizationError({ errors: [{ message: 'Parse failed near "unauthorized"' }] })).toBe(false);
  });
});

describe('describeApiError', () => {
  it('gives a 403 copy the reader can act on, without the group list', () => {
    const message = describeApiError(dispatcherDenial('listDocuments'), 'list documents');
    expect(message).toBe('You do not have permission to list documents. If your account is new, ask an administrator to assign it a role.');
    expect(message).not.toContain('Annotator');
    expect(message).not.toContain('Please try again');
  });

  it('keeps the server detail for everything else', () => {
    expect(describeApiError({ errors: [{ message: 'DynamoDB throttled' }] }, 'list documents')).toBe(
      'Failed to list documents: DynamoDB throttled',
    );
  });

  it('never renders [object Object]', () => {
    expect(describeApiError({ unexpected: true }, 'list documents')).not.toContain('[object Object]');
  });
});

describe('FILE_ACCESS_DENIED_MESSAGE', () => {
  it('does not tell the user to retry', () => {
    expect(FILE_ACCESS_DENIED_MESSAGE).not.toMatch(/try again/i);
    expect(FILE_ACCESS_DENIED_MESSAGE).toMatch(/administrator/);
  });
});
