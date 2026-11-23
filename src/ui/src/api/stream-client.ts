// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Signed streaming client for chat. Replaces the AppSync mutation+subscription
// chat flow under the httpapi transport (AppSync is unavailable in GovCloud and
// not FedRAMP-compliant).
//
// It POSTs to a Lambda Function URL (AuthType=AWS_IAM, InvokeMode=RESPONSE_STREAM),
// SigV4-signing the request with the authenticated Cognito Identity Pool
// credentials, then reads the streamed Server-Sent-Events response body
// incrementally and invokes a callback for each parsed event.
//
// The signature proves only that the caller is a signed-in user of the pool, so
// the request also carries the user's Cognito ID token in the `x-idp-id-token`
// header (`authorization` holds the signature). The endpoint verifies that token
// and takes the caller's identity — whose chat session and memory this is — from
// it, and from nothing in the request body. The event shapes
// mirror exactly what the AppSync subscriptions delivered, so the existing UI
// state machines (ChatPanel.handleUpdate, useAgentChat.addMessage) are reused
// unchanged — only the source of events differs.
//
// Works identically whether the SPA is served via CloudFront or ALB: the
// Function URL is addressed directly by the browser.
import { SignatureV4 } from '@smithy/signature-v4';
import { HttpRequest } from '@smithy/protocol-http';
import { Sha256 } from '@aws-crypto/sha256-browser';
import { ConsoleLogger } from 'aws-amplify/utils';

import { streamUrl, awsRegion } from '../aws-exports';
import { fetchSharedAuthSession } from './auth-session';

const logger = new ConsoleLogger('streamClient');

/** AWS credentials shape (subset) — what Amplify's fetchAuthSession exposes. */
export interface StreamCredentials {
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken?: string;
}

/** A single decoded SSE event payload (the JSON after `data:`). */
export type StreamEvent = Record<string, unknown>;

export interface StreamChatOptions {
  /** Route path on the Function URL, e.g. '/chat/document' or '/chat/agent'. */
  path: string;
  /** JSON request body. */
  body: Record<string, unknown>;
  /** Cognito Identity Pool credentials (from useCurrentSessionCreds). */
  credentials: StreamCredentials;
  /** Called once per decoded SSE event. */
  onEvent: (event: StreamEvent) => void;
  /** Optional AbortSignal to cancel the stream. */
  signal?: AbortSignal;
}

/** Header the endpoint reads the caller's Cognito ID token from. */
export const ID_TOKEN_HEADER = 'x-idp-id-token';

const getIdToken = async (): Promise<string> => {
  const session = await fetchSharedAuthSession();
  const token = session.tokens?.idToken?.toString();
  if (!token) {
    throw new Error('streamClient: no Cognito id token available');
  }
  return token;
};

const getStreamEndpoint = (): URL => {
  if (!streamUrl) {
    throw new Error('streamClient: VITE_STREAM_URL is not configured');
  }
  return new URL(streamUrl);
};

/**
 * SigV4-sign and POST to the Function URL, then read the SSE response stream,
 * splitting on blank lines and parsing `data:` payloads. Resolves when the
 * stream ends; rejects on network/HTTP/abort error.
 */
export const streamChat = async ({ path, body, credentials, onEvent, signal }: StreamChatOptions): Promise<void> => {
  const endpoint = getStreamEndpoint();
  const payload = JSON.stringify(body);
  const idToken = await getIdToken();

  // Build a canonical request for SigV4. Function URLs are signed against the
  // 'lambda' service. We must include the body hash (x-amz-content-sha256) so
  // the signed payload matches what we actually send.
  const request = new HttpRequest({
    method: 'POST',
    protocol: endpoint.protocol,
    hostname: endpoint.hostname,
    path: endpoint.pathname.replace(/\/$/, '') + path,
    headers: {
      'content-type': 'application/json',
      host: endpoint.hostname,
      [ID_TOKEN_HEADER]: idToken,
    },
    body: payload,
  });

  const signer = new SignatureV4({
    service: 'lambda',
    region: awsRegion ?? '',
    credentials: {
      accessKeyId: credentials.accessKeyId,
      secretAccessKey: credentials.secretAccessKey,
      sessionToken: credentials.sessionToken,
    },
    sha256: Sha256,
  });

  const signed = await signer.sign(request);

  const url = `${endpoint.protocol}//${endpoint.hostname}${request.path}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: signed.headers as Record<string, string>,
      body: payload,
      signal,
    });
  } catch (e) {
    if (signal?.aborted) return;
    const message = e instanceof Error ? e.message : String(e);
    throw new Error(`streamClient: request failed: ${message}`);
  }

  if (!response.ok || !response.body) {
    const text = await response.text().catch(() => '');
    throw new Error(`streamClient: HTTP ${response.status} ${text}`.trim());
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const flushFrame = (frame: string): void => {
    // An SSE frame may contain multiple `data:` lines; concatenate them.
    const dataLines = frame
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice('data:'.length).trimStart());
    if (dataLines.length === 0) return;
    const json = dataLines.join('\n');
    if (!json) return;
    try {
      onEvent(JSON.parse(json) as StreamEvent);
    } catch (e) {
      logger.warn('streamClient: failed to parse SSE frame', e, json);
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Frames are separated by a blank line ("\n\n").
      let sepIndex = buffer.indexOf('\n\n');
      while (sepIndex !== -1) {
        const frame = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        flushFrame(frame);
        sepIndex = buffer.indexOf('\n\n');
      }
    }
    // Flush any trailing frame without a terminating blank line.
    if (buffer.trim()) flushFrame(buffer);
  } catch (e) {
    if (signal?.aborted) return;
    throw e;
  }
};

export default streamChat;
