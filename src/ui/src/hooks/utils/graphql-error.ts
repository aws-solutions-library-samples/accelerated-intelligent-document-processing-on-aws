// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Extract a human-readable error message from whatever shape Amplify / AppSync
 * threw at us.
 *
 * Amplify's GraphQL client throws an object like:
 *
 *   { errors: [
 *       { message: "Not Authorized to access subscribeFeature on type Mutation",
 *         path: ["subscribeFeature"],
 *         ... },
 *       ...
 *     ],
 *     data: null,
 *   }
 *
 * Passing that through `String(e)` or `new Error(String(e))` yields the
 * useless string `"[object Object]"`, which is what users saw in red error
 * banners. This helper drills into the common shapes and returns a sensible
 * multi-line message; falls back to `JSON.stringify` as a last resort so the
 * UI never shows `[object Object]` again.
 */
export function extractGraphQLErrorMessage(err: unknown): string {
  // Native Error or anything with a usable .message
  if (err instanceof Error && err.message) {
    return err.message;
  }

  if (typeof err === 'string') {
    return err;
  }

  if (err && typeof err === 'object') {
    const anyErr = err as {
      errors?: Array<{ message?: string; errorType?: string; path?: unknown[] }>;
      message?: string;
    };

    // Amplify GraphQL error envelope — most common path.
    if (Array.isArray(anyErr.errors) && anyErr.errors.length > 0) {
      const messages = anyErr.errors.map((e) => e?.message).filter((m): m is string => typeof m === 'string' && m.length > 0);
      if (messages.length > 0) {
        return messages.join('\n');
      }
    }

    // Single-message fallback (e.g., a plain { message: '...' }).
    if (typeof anyErr.message === 'string' && anyErr.message.length > 0) {
      return anyErr.message;
    }

    // Last-resort JSON so the UI at least shows the shape rather than "[object Object]".
    try {
      return JSON.stringify(err);
    } catch {
      /* fall through */
    }
  }

  return 'Unknown error';
}

/**
 * True when the API refused the call for lack of permission.
 *
 * The REST client throws the dispatcher's body verbatim and discards the HTTP
 * status, so `errorType: "Unauthorized"` is the only reliable signal — the same
 * marker the dispatcher sets for both its own group check and a resolver's
 * `PermissionError`. The message-substring arm is a fallback for the resolvers that
 * still raise a bare error whose text begins `Unauthorized`/`Forbidden`.
 */
export function isAuthorizationError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false;
  const envelope = err as {
    errors?: Array<{ message?: string; errorType?: string }>;
    errorType?: string;
    message?: string;
  };
  const candidates = [...(Array.isArray(envelope.errors) ? envelope.errors : []), envelope];
  return candidates.some((e) => {
    if (e?.errorType === 'Unauthorized' || e?.errorType === 'Forbidden') return true;
    const text = (e?.message ?? '').toLowerCase();
    return text.startsWith('unauthorized') || text.startsWith('forbidden') || text.includes('access denied');
  });
}

/**
 * The message to show the user for a failed call, with permission failures given
 * copy they can act on.
 *
 * Interpolating the server's text was actively unhelpful for a 403: the banner read
 * `Failed to list documents (Unauthorized): Unauthorized: listDocuments requires
 * one of [...]`, which names Cognito groups the reader has no way to grant
 * themselves and does not say what to do. Several viewers went further and offered
 * "Please try again", which for an authorization failure is advice that can never
 * work. `action` names what was being attempted, in the user's terms — "list
 * documents", "load this document".
 */
/**
 * What the file viewers show when the content reads are refused.
 *
 * One string, shared, because the four viewers each had their own "Failed to load
 * X. Please try again." and fixing one of them would have left the others giving
 * advice that cannot work.
 */
export const FILE_ACCESS_DENIED_MESSAGE =
  'You do not have permission to view this file. If your account is new, ask an administrator to assign it a role.';

export function describeApiError(err: unknown, action: string): string {
  if (isAuthorizationError(err)) {
    return `You do not have permission to ${action}. If your account is new, ask an administrator to assign it a role.`;
  }
  return `Failed to ${action}: ${extractGraphQLErrorMessage(err)}`;
}
