// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Thin REST client that replaces AWS AppSync (GraphQL over Amplify) for
// UI<->backend queries and mutations. AppSync is unavailable in GovCloud and
// not FedRAMP-compliant; this client talks to an API Gateway HTTP API instead.
//
// It deliberately mimics the small slice of the Amplify `generateClient()` API
// the app uses, so call sites stay unchanged:
//
//   const client = generateClient();
//   const res = await client.graphql({ query: listDocuments, variables });
//   res.data.listDocuments  // <- same shape as Amplify returns
//
// Queries/mutations are POSTed to `${apiBaseUrl}/op/<fieldName>` with the
// Cognito id token as the Authorization header. The HTTP API JWT authorizer
// validates the token; the dispatcher Lambda routes by field name.
//
// Subscriptions have no transport here (the HTTP API uses polling instead).
// `graphql()` on a subscription returns an inert subscription object so any
// not-yet-migrated call site fails safe (logs once) rather than throwing.
import { fetchSharedAuthSession } from './auth-session';
import { ConsoleLogger } from 'aws-amplify/utils';

import { apiBaseUrl } from '../aws-exports';

const logger = new ConsoleLogger('restClient');

// Match the operation kind + field name from a generated GraphQL op string.
//   "  query ListDocuments($x: Int) {\n  listDocuments(...) { ... } }"
// -> kind = "query", field = "listDocuments"
// Also captures the top-level field's argument list so we can map GraphQL
// argument names to their $variable names (see argMap below).
const OP_RE = /\b(query|mutation|subscription)\s+\w+\s*(?:\([^)]*\))?\s*\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(([^)]*)\))?/;

// Extract "ArgName: $varName" pairs from a field's argument list.
const ARG_RE = /([A-Za-z_]\w*)\s*:\s*\$([A-Za-z_]\w*)/g;

const fieldCache = new Map<string, ParsedOp>();

interface ParsedOp {
  kind: string;
  field: string;
  // GraphQL-argument-name -> $variable-name for the top-level field. AppSync
  // exposed $ctx.arguments keyed by ARGUMENT name, not variable name; the two
  // usually match but not always (e.g. getDocument(ObjectKey: $objectKey)), so
  // we must remap before sending or the resolver sees the wrong key.
  argMap: Record<string, string>;
}

const parseOperation = (query: string): ParsedOp => {
  const cached = fieldCache.get(query);
  if (cached) return cached;
  const m = OP_RE.exec(query);
  if (!m) {
    throw new Error('restClient: could not parse operation from query string');
  }
  const argMap: Record<string, string> = {};
  const argList = m[3] ?? '';
  let a: RegExpExecArray | null;
  ARG_RE.lastIndex = 0;
  while ((a = ARG_RE.exec(argList)) !== null) {
    argMap[a[1]] = a[2];
  }
  const parsed = { kind: m[1], field: m[2], argMap };
  fieldCache.set(query, parsed);
  return parsed;
};

// Build the `arguments` object the resolver expects (keyed by GraphQL argument
// name) from the caller's `variables` (keyed by $variable name). When the
// operation declares no field args, or a variable has no matching arg, pass it
// through unchanged so nothing is silently dropped.
const buildArguments = (argMap: Record<string, string>, variables: Record<string, unknown> | undefined): Record<string, unknown> => {
  const vars = variables ?? {};
  const mappedVarNames = new Set(Object.values(argMap));
  const out: Record<string, unknown> = {};
  // 1) Remap declared field args: argName <- variables[varName].
  for (const [argName, varName] of Object.entries(argMap)) {
    if (varName in vars) out[argName] = vars[varName];
  }
  // 2) Pass through any variables not consumed by the arg map (arg name ==
  //    variable name is the common case and is covered by (1); this keeps any
  //    extras that weren't declared in the parsed arg list).
  for (const [k, v] of Object.entries(vars)) {
    if (!mappedVarNames.has(k) && !(k in out)) out[k] = v;
  }
  return out;
};

const getIdToken = async (): Promise<string> => {
  const session = await fetchSharedAuthSession();
  const token = session.tokens?.idToken?.toString();
  if (!token) {
    throw new Error('restClient: no Cognito id token available');
  }
  return token;
};

// Shape returned to callers — mirrors Amplify's { data, errors }.
interface GraphqlResult<T = unknown> {
  data: T;
  errors?: { message?: string; errorType?: string }[];
}

interface GraphqlRequest {
  query: string;
  variables?: Record<string, unknown>;
  authMode?: string;
}

// Inert subscription returned for subscription operations (polling replaces
// real-time push under the HTTP API transport).
const inertSubscription = (field: string) => ({
  subscribe: () => {
    logger.warn(`restClient: subscription '${field}' is a no-op under httpapi transport (use polling)`);
    return { unsubscribe: () => {} };
  },
});

const doGraphqlRequest = async ({ query, variables }: GraphqlRequest): Promise<GraphqlResult> => {
  const { field, argMap } = parseOperation(query);

  if (!apiBaseUrl) {
    throw new Error('restClient: VITE_API_BASE_URL is not configured');
  }

  const token = await getIdToken();
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl}/op/${field}`, {
      method: 'POST',
      headers: {
        Authorization: token,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ arguments: buildArguments(argMap, variables) }),
    });
  } catch (e) {
    // Network error — surface in the same { errors: [...] } shape callers parse.
    const message = e instanceof Error ? e.message : String(e);
    throw { errors: [{ message, errorType: 'NetworkError' }] };
  }

  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }

  if (!response.ok) {
    // Dispatcher returns { errors: [{ message, errorType }] }; pass it through
    // so existing error handling (gqlError.errors[0]) keeps working.
    if (body && typeof body === 'object' && 'errors' in body) {
      throw body;
    }
    throw {
      errors: [{ message: `Request failed (${response.status})`, errorType: 'HttpError' }],
    };
  }

  // Success: wrap under the field name to match Amplify's res.data.<field>.
  return { data: { [field]: body } };
};

// Synchronous dispatcher. Amplify's client.graphql() returns an Observable
// (with .subscribe()) *synchronously* for subscription ops and a Promise for
// queries/mutations. Subscription call sites do `client.graphql(...).subscribe(...)`
// with no await, so we MUST return the inert subscription synchronously here —
// returning a Promise (e.g. from an async function) would make `.subscribe`
// undefined and throw "subscribe is not a function", crashing the page render.
const doGraphql = (req: GraphqlRequest): Promise<GraphqlResult> | ReturnType<typeof inertSubscription> => {
  const { kind, field } = parseOperation(req.query);
  if (kind === 'subscription') {
    return inertSubscription(field);
  }
  return doGraphqlRequest(req);
};

// Brand types emitted by the codegen for each operation. We extract the result
// type from the brand so `.graphql(op)` returns a precisely-typed `{ data }`,
// exactly as the Amplify client did — keeping call-site inference (e.g.
// `result.data.listX.items.map(...)`) without referencing AppSync.
import type { GeneratedQuery, GeneratedMutation, GeneratedSubscription } from '@aws-amplify/api-graphql';

// Inert subscription handle returned for subscription ops (polling replaces them).
interface SubscriptionHandle {
  subscribe: (observer?: unknown) => { unsubscribe: () => void };
}

// The object returned by our generateClient() replacement. Only `.graphql()`
// is used from the old Amplify client. Overloads mirror Amplify's typing so a
// query/mutation resolves to `{ data: Result }` and a subscription returns an
// (inert) subscription handle.
export interface RestGraphqlClient {
  graphql<Result, Variables>(options: {
    query: GeneratedQuery<Variables, Result> | GeneratedMutation<Variables, Result>;
    variables?: Variables;
    authMode?: string;
  }): Promise<{ data: Result; errors?: { message?: string; errorType?: string }[] }>;
  graphql<Result, Variables>(options: {
    query: GeneratedSubscription<Variables, Result>;
    variables?: Variables;
    authMode?: string;
  }): SubscriptionHandle;
  // Fallback for raw string queries (rarely used).
  graphql(req: GraphqlRequest): Promise<GraphqlResult>;
}

export const createRestClient = (): RestGraphqlClient =>
  ({
    // doGraphql implements all overloads; the cast bridges the runtime impl to
    // the typed surface above.
    graphql: doGraphql,
  }) as unknown as RestGraphqlClient;

export default createRestClient;
