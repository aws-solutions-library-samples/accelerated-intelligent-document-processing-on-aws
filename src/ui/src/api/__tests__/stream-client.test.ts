// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// aws-exports reads import.meta.env at module load; provide the values the
// stream client needs before importing it.
vi.mock('../../aws-exports', () => ({
  streamUrl: 'https://abc123.lambda-url.us-west-2.on.aws/',
  awsRegion: 'us-west-2',
}));

// SignatureV4 hits WebCrypto via Sha256; stub the signer so the test stays in
// pure JS (we only care about the SSE parsing, not the signature bytes).
vi.mock('@smithy/signature-v4', () => ({
  SignatureV4: class {
    async sign(req: { headers: Record<string, string> }) {
      return { ...req, headers: { ...req.headers, authorization: 'AWS4-HMAC-SHA256 ...' } };
    }
  },
}));

// The caller's ID token comes from the shared Amplify session. Mutable so a case
// can simulate a signed-out session.
const authState: { idToken: string | undefined } = { idToken: 'header.payload.signature' };
vi.mock('../auth-session', () => ({
  fetchSharedAuthSession: async () => ({
    tokens: authState.idToken ? { idToken: { toString: () => authState.idToken } } : undefined,
  }),
}));

import { ID_TOKEN_HEADER, streamChat, type StreamEvent } from '../stream-client';

// Build a ReadableStream that emits the given UTF-8 string chunks in order.
const streamFrom = (chunks: string[]): ReadableStream<Uint8Array> => {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i]));
        i += 1;
      } else {
        controller.close();
      }
    },
  });
};

const creds = { accessKeyId: 'AKIA', secretAccessKey: 'secret', sessionToken: 'token' };

describe('streamChat', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    authState.idToken = 'header.payload.signature';
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('parses SSE events split arbitrarily across chunks', async () => {
    const body = streamFrom([
      'data: {"method":"assistant_status","sta',
      'tus":"CALLING_MODEL","content":"Querying"}\n\n',
      'data: {"method":"assistant_stream","content":"Hel"}\n\ndata: {"method":"assistant_stream","content":"lo"}\n\n',
      'data: {"method":"assistant_final","content":"Hello","isProcessing":false}\n\n',
    ]);
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(body, { status: 200 })),
    );

    const events: StreamEvent[] = [];
    await streamChat({
      path: '/chat/document',
      body: { sessionId: 's1', prompt: 'hi' },
      credentials: creds,
      onEvent: (e) => events.push(e),
    });

    expect(events.map((e) => e.method)).toEqual(['assistant_status', 'assistant_stream', 'assistant_stream', 'assistant_final']);
    expect(events[0].status).toBe('CALLING_MODEL');
    expect(events[3].isProcessing).toBe(false);
  });

  it('flushes a trailing frame without a terminating blank line', async () => {
    const body = streamFrom(['data: {"method":"assistant_final","content":"done"}']);
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(body, { status: 200 })),
    );

    const events: StreamEvent[] = [];
    await streamChat({
      path: '/chat/agent',
      body: { sessionId: 's2', prompt: 'hi' },
      credentials: creds,
      onEvent: (e) => events.push(e),
    });

    expect(events).toHaveLength(1);
    expect(events[0].content).toBe('done');
  });

  it('sends the Cognito ID token in its own header, alongside the signature', async () => {
    const fetchMock = vi.fn(async () => new Response(streamFrom(['data: {"method":"assistant_final"}\n\n']), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await streamChat({
      path: '/chat/agent',
      body: { sessionId: 's3', prompt: 'hi' },
      credentials: creds,
      onEvent: () => {},
    });

    expect(ID_TOKEN_HEADER).toBe('x-idp-id-token');
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    const headers = init.headers as Record<string, string>;
    expect(headers[ID_TOKEN_HEADER]).toBe('header.payload.signature');
    // The signature stays in `authorization`; the token does not replace it.
    expect(headers.authorization).toMatch(/^AWS4-HMAC-SHA256/);
  });

  it('refuses to send a request when there is no ID token', async () => {
    authState.idToken = undefined;
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      streamChat({
        path: '/chat/agent',
        body: { sessionId: 's4', prompt: 'hi' },
        credentials: creds,
        onEvent: () => {},
      }),
    ).rejects.toThrow(/no Cognito id token/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('throws on non-OK HTTP status', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('boom', { status: 403 })),
    );

    await expect(
      streamChat({
        path: '/chat/document',
        body: {},
        credentials: creds,
        onEvent: () => {},
      }),
    ).rejects.toThrow(/403/);
  });
});
