// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

//
// Non-streaming chat delivery via polling — used when the Lambda Function URL
// streaming endpoint is unavailable (e.g. AWS GovCloud, where Lambda Function
// URLs do not exist, so VITE_STREAM_URL is empty).
//
// The chat processors persist ONLY the final assistant message to the
// ChatMessages table (there are no intermediate/streaming checkpoints), so this
// path gives a "spinner, then the full answer" experience rather than
// token-by-token streaming. The send-message mutation and the getChatMessages
// query both route through the existing Cognito-authed REST API (/op), so no
// Lambda Function URL is involved.
//
// Flow:
//   1. Caller captures a BASELINE of the assistant messages already in the
//      session (fetchAssistantKeys) — clock-independent.
//   2. Caller sends the chat mutation (sendAgentChatMessage /
//      sendChatDocumentMessage) — this async-invokes the processor.
//   3. pollForAssistantReply() polls getChatMessages until an assistant message
//      NOT in the baseline appears with isProcessing=false, then returns it.
//      Times out after maxWaitMs. A non-transient (auth) error is surfaced
//      immediately rather than swallowed.
//
// Why baseline keys instead of a timestamp bound: the user-prompt timestamp is
// a CLIENT wall-clock value while the assistant timestamp is written with the
// SERVER wall-clock. Comparing them is unsafe under clock skew (a fast client
// clock would filter out every real reply). Diffing against the set of
// pre-existing assistant messages avoids any cross-clock comparison.
//

import { getChatMessages } from '../graphql/generated';
import { isAuthorizationError } from '../hooks/utils/graphql-error';

export interface PolledChatMessage {
  role: string;
  content: string;
  timestamp: string;
  isProcessing?: boolean | null;
  sessionId?: string | null;
  messageType?: string | null;
  toolMetadata?: unknown;
}

/**
 * Structural type for the REST client's `.graphql()` — kept loose (`any` args)
 * so the app's precisely-overloaded RestGraphqlClient is assignable here.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type GraphqlClient = { graphql: (args: any) => Promise<any> };

const ASSISTANT_ROLE = 'assistant';

/** Stable identity of an assistant message for baseline-diffing. */
const assistantKey = (m: PolledChatMessage): string => `${m.timestamp}|${m.content?.length ?? 0}`;

/**
 * True for errors that must NOT be retried (auth/authorization failures).
 *
 * Delegates to the shared predicate rather than keeping a second copy. The copy this
 * replaces matched `access denied` anywhere in the joined message blob, so a
 * server-side S3 or KMS denial — which `get_file_contents_resolver` surfaces as
 * `errorType: "InternalError"` with the text `Error accessing S3: Access Denied` —
 * aborted the poll as an authorization failure. The shared predicate skips the
 * substring arm whenever a non-authorization `errorType` is stated, so such a fault
 * is now retried until the deadline like any other transient read error, which is
 * what the loop below is for.
 */
const isAuthError = (err: unknown): boolean => isAuthorizationError(err);

const sleep = (ms: number, signal?: AbortSignal): Promise<void> =>
  new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const t = setTimeout(resolve, ms);
    signal?.addEventListener(
      'abort',
      () => {
        clearTimeout(t);
        reject(new DOMException('Aborted', 'AbortError'));
      },
      { once: true },
    );
  });

const fetchMessages = async (client: GraphqlClient, sessionId: string): Promise<PolledChatMessage[]> => {
  const response = await client.graphql({ query: getChatMessages, variables: { sessionId } });
  return (response.data?.getChatMessages as PolledChatMessage[] | undefined) ?? [];
};

/**
 * Snapshot the keys of assistant messages already present in the session, to
 * use as the poll baseline. Returns an empty set if the read fails (worst case
 * the poll may match a pre-existing reply, but the fresh reply is newer and the
 * newest-first sort prefers it).
 */
export const fetchAssistantKeys = async (client: GraphqlClient, sessionId: string): Promise<Set<string>> => {
  try {
    const messages = await fetchMessages(client, sessionId);
    return new Set(messages.filter((m) => m.role === ASSISTANT_ROLE).map(assistantKey));
  } catch {
    return new Set<string>();
  }
};

interface PollOptions {
  /** Amplify-compatible client exposing `.graphql({ query, variables })`. */
  client: GraphqlClient;
  sessionId: string;
  /**
   * Keys of assistant messages that existed BEFORE this turn was sent (from
   * fetchAssistantKeys). The reply is the first assistant message NOT in this
   * set. Clock-independent — no client/server timestamp comparison.
   */
  knownAssistantKeys: Set<string>;
  /** Poll interval in ms (default 2000). */
  intervalMs?: number;
  /** Max total wait in ms before giving up (default 300000 = 5 min). */
  maxWaitMs?: number;
  /** Optional AbortSignal to cancel polling (component unmount / session switch). */
  signal?: AbortSignal;
}

/**
 * Poll getChatMessages until the assistant's reply to this turn (an assistant
 * message not in `knownAssistantKeys`, no longer processing) is persisted, and
 * return it. Resolves with `null` on timeout. Throws on abort or on a
 * non-transient auth error.
 */
export const pollForAssistantReply = async ({
  client,
  sessionId,
  knownAssistantKeys,
  intervalMs = 2000,
  maxWaitMs = 300000,
  signal,
}: PollOptions): Promise<PolledChatMessage | null> => {
  const deadline = Date.now() + maxWaitMs;

  while (Date.now() < deadline) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');

    let messages: PolledChatMessage[] = [];
    try {
      messages = await fetchMessages(client, sessionId);
    } catch (err) {
      // Surface auth failures immediately (e.g. session-ownership denial) —
      // retrying can never succeed and would waste the full timeout. Other
      // (transient) read errors keep polling until the deadline.
      if (isAuthError(err)) throw err;
      messages = [];
    }

    // The newest assistant message that is new this turn and finished.
    const reply = messages
      .filter((m) => m.role === ASSISTANT_ROLE && m.isProcessing !== true && !knownAssistantKeys.has(assistantKey(m)))
      .sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1))[0];

    if (reply) return reply;

    await sleep(intervalMs, signal);
  }

  return null;
};
