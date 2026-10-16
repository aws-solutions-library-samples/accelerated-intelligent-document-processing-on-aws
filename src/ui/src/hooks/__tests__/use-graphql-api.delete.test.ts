// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A delete that did not delete has to reach the error banner, with its cause.
 *
 * This is the last link in the #1366 chain and the one closest to the user. The API
 * answered HTTP 200 with a body of `false`, `deleteDocuments` returned that value, and
 * both callers — the Document List's confirm handler and the detail page's — logged it
 * at debug level and then closed the modal or navigated away. So two faults the
 * resolver had written to its own log reached the browser as an untroubled success.
 *
 * Two arms are covered because the resolver's contract changed and the transport's did
 * not. It now raises, which Amplify surfaces as a thrown error envelope — that is the
 * live path. A falsy *returned* value is no longer produced, and is asserted anyway:
 * relying on the resolver's contract to keep a UI failure visible is what put this
 * defect in front of a user in the first place.
 *
 * ⚠️ The message matters as much as the banner. "Please try again later" was the old
 * copy, and for the shapes this fails with — a missing IAM action, a request the
 * service refuses — a retry can never work, so the advice was worse than silence:
 * it sent the reader back to the same button.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, cleanup, renderHook } from '@testing-library/react';

// Hoisted: the hook builds its GraphQL client at module scope, before ordinary
// top-level consts in this file are initialized.
const { graphql, setErrorMessage } = vi.hoisted(() => ({ graphql: vi.fn(), setErrorMessage: vi.fn() }));

vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('../../contexts/app', () => ({ default: () => ({ setErrorMessage }) }));
vi.mock('../use-polling', () => ({ default: () => undefined, usePolling: () => undefined }));
vi.mock('../../graphql/generated', () => ({
  listDocuments: 'listDocuments',
  getDocumentCount: 'getDocumentCount',
  getDocument: 'getDocument',
  deleteDocument: 'deleteDocument',
  reprocessDocument: 'reprocessDocument',
  abortWorkflow: 'abortWorkflow',
}));

import useGraphQlApi from '../use-graphql-api';

/** What the dispatcher sends for a resolver that raised, as Amplify throws it. */
const dispatcherFailure = (message: string) => ({
  errors: [{ message, errorType: 'InternalError' }],
});

const RESOLVER_MESSAGE =
  '1 of 1 document(s) could not be completely deleted (0 completed) — invoices/march.pdf: ' +
  'Error in list entry deletion: An error occurred (ValidationException) when calling the Query operation';

const deleteOnce = async (keys: string[]) => {
  const hook = renderHook(() => useGraphQlApi({ initialPeriodsToLoad: 1 }));
  let answer: unknown;
  await act(async () => {
    answer = await hook.result.current.deleteDocuments(keys);
  });
  return answer;
};

describe('deleteDocuments', () => {
  beforeEach(() => {
    graphql.mockReset();
    setErrorMessage.mockReset();
  });
  afterEach(() => cleanup());

  it('shows the banner when the resolver raised, carrying the cause', async () => {
    graphql.mockRejectedValue(dispatcherFailure(RESOLVER_MESSAGE));

    await deleteOnce(['invoices/march.pdf']);

    expect(setErrorMessage).toHaveBeenCalledTimes(1);
    const shown = setErrorMessage.mock.calls[0][0] as string;
    // The document and the cause, so the reader can act. Without the cause this is
    // the same opacity as the 200 was, one status code further on.
    expect(shown).toContain('invoices/march.pdf');
    expect(shown).toContain('ValidationException');
  });

  it('does not tell the reader to try again later', async () => {
    graphql.mockRejectedValue(dispatcherFailure(RESOLVER_MESSAGE));

    await deleteOnce(['invoices/march.pdf']);

    const shown = (setErrorMessage.mock.calls[0][0] as string).toLowerCase();
    expect(shown).not.toContain('try again');
  });

  it('does not mistake a server-side AccessDenied for the caller lacking permission', async () => {
    // ⚠️ The likeliest delete failure contains the literal words "Access Denied" — an
    // IAM denial of the *resolver's own* execution role — and `isAuthorizationError`
    // has a prose arm matching that substring. If it fired, the carefully reported
    // cause would be replaced by "ask an administrator to assign it a role", which is
    // advice the reader cannot act on and is exactly the wrongness this change removed.
    //
    // It does not fire, because an explicit non-authorization `errorType` short-circuits
    // the prose arm. That is a property of two files agreeing, so it is pinned here
    // rather than left to be re-derived.
    graphql.mockRejectedValue({
      errors: [
        {
          message:
            '1 of 1 document(s) could not be completely deleted (0 completed) — a.pdf: ' +
            'Error deleting from output bucket: An error occurred (AccessDenied) when ' +
            'calling the ListObjectVersions operation',
          errorType: 'InternalError',
        },
      ],
    });

    await deleteOnce(['a.pdf']);

    const shown = setErrorMessage.mock.calls[0][0] as string;
    expect(shown).toContain('AccessDenied');
    expect(shown).not.toContain('ask an administrator');
  });

  it('names the action in the banner for an authorization refusal', async () => {
    // `describeApiError` deliberately replaces the server's text for a 403: the
    // Cognito groups it names are not something the reader can grant themselves.
    graphql.mockRejectedValue({
      errors: [{ message: 'Unauthorized: deleteDocument requires Admin or Author group', errorType: 'Unauthorized' }],
    });

    await deleteOnce(['invoices/march.pdf']);

    const shown = setErrorMessage.mock.calls[0][0] as string;
    expect(shown).toContain('delete document(s)');
    expect(shown).toContain('administrator');
  });

  it('still surfaces a falsy result, rather than trusting the resolver to raise', async () => {
    graphql.mockResolvedValue({ data: { deleteDocument: false } });

    const answer = await deleteOnce(['invoices/march.pdf']);

    expect(answer).toBe(false);
    expect(setErrorMessage).toHaveBeenCalledTimes(1);
    expect((setErrorMessage.mock.calls[0][0] as string).toLowerCase()).toContain('did not complete');
  });

  it('says nothing on a successful delete', async () => {
    graphql.mockResolvedValue({ data: { deleteDocument: true } });

    const answer = await deleteOnce(['invoices/march.pdf']);

    expect(answer).toBe(true);
    expect(setErrorMessage).not.toHaveBeenCalled();
  });
});
