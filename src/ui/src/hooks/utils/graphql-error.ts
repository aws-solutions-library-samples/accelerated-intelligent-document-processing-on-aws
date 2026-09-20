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
 * True when the API refused the call because the CALLER lacks permission.
 *
 * The REST client throws the dispatcher's body verbatim and discards the HTTP
 * status, so `errorType: "Unauthorized"` is the only reliable signal — the marker
 * the dispatcher sets both for its own group check and for a resolver's
 * `PermissionError`.
 *
 * Two things about the message arm, which is a fallback for resolvers that raise a
 * bare error and rely on the dispatcher's message-prefix mapping.
 *
 * The `access denied` substring is a **forward-looking** guard, not a current
 * dependency. The configuration and sync resolvers report an out-of-scope
 * configuration version **in band**, as HTTP 200 with a body whose message begins
 * "Access denied:", and that wording is the only thing identifying it — but
 * `rest-client.ts` throws only when `!response.ok`, so an in-band denial comes back
 * as `data` and never reaches this function, and none of this helper's six call
 * sites is a configuration or sync path. Everything that does throw today carries an
 * `errorType`. Keep the arm for the resolver that raises a bare message and relies on
 * the dispatcher's prefix mapping; do not claim anything currently depends on it.
 *
 * ⚠️ The same substring also matches a **server-side** IAM failure. A denial of
 * `get_file_contents_resolver`'s *own* execution role, the bucket policy or the KMS
 * key is a server defect no role of the user's can fix, and S3's own wording for it
 * is literally "Access Denied" — so telling the user to ask an administrator for a
 * role would be confidently wrong. Two things keep the two apart. That resolver no
 * longer returns S3's message (it answers "This deployment could not read the
 * requested file", with the S3 detail in its log), and the message arm here is
 * skipped whenever the envelope names an error type that is not an authorization
 * one: an explicit type is better evidence than a substring of prose. A server-side
 * S3 failure carries `errorType: "InternalError"`, so it takes the type arm.
 *
 * What the resolver DOES send as an authorization refusal is its bucket allow-list
 * check — `PermissionError` -> HTTP 403 `errorType: "Unauthorized"`, so it reaches
 * FILE_ACCESS_DENIED_MESSAGE below. That copy names a role because the reachable
 * case is a caller whose groups do not cover the operation; the allow-list case
 * needs an `s3Uri` naming a bucket outside the deployment, which this UI never
 * builds.
 */
const AUTH_ERROR_TYPES = new Set(['Unauthorized', 'Forbidden']);

export function isAuthorizationError(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false;
  const envelope = err as {
    errors?: Array<{ message?: string; errorType?: string }>;
    errorType?: string;
    message?: string;
  };
  const candidates = [...(Array.isArray(envelope.errors) ? envelope.errors : []), envelope];
  return candidates.some((e) => {
    // Only `Unauthorized` is emitted by the backend today; `Forbidden` is carried
    // for the dispatcher's documented message-prefix contract, not because
    // anything sets it.
    if (e?.errorType && AUTH_ERROR_TYPES.has(e.errorType)) return true;
    // A stated non-authorization type wins over the prose. `InternalError` is the
    // case that matters (see above); any other explicit type is equally not ours.
    if (e?.errorType) return false;
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
