---
paths:
  - "nested/api-resolvers/**"
  - "src/lambda/**"
  - "src/ui/src/graphql/**"
  - "scripts/api_rbac_expectations.yaml"
---

# API authorization rules

Covers authorization for UI API operations: the default-deny dispatcher behind
`POST /op/{field}`, the group and config-version-scope checks in each resolver,
and the Function URL routes that bypass the dispatcher.

## Must
- Do all of the following when you add an operation:
  - Add a server-side group check (and scope check, if needed) to the resolver.
  - Add an `@aws_cognito_user_pools` directive in `schema.graphql`.
  - Declare the operation's groups in `scripts/api_rbac_expectations.yaml`.
  - Regenerate and commit the manifest with
    `python3 scripts/sdlc/generate_api_rbac_manifest.py`.
- Pick the weakest policy that is still correct: explicit groups, `ANY_GROUP`,
  `ANY` or `IAM_ONLY`. Write a reason that holds for that one operation, based
  on what its payload leads to as well as what it literally returns.
- Read the caller's groups from the verified JWT claims
  (`identity.claims['cognito:groups']`), never from the request body.
- If an operation is also reachable without going through the REST route (a
  Function URL or a direct invoke), put the group check in the component that does
  the work and declare the route under `function_url_endpoints:`.
- On a Function URL route, the identity verified by the transport takes
  precedence. Refuse a request whose body contradicts it. Never use a body
  `callerSub` as an authorization key.
- Signal each refusal with the right exception: `PermissionError` for an
  authorization refusal, `ValueError` for a bad argument, and `ResourceNotFound`
  for a missing object.
- Log a denial at WARNING with a "Denied", "Forbidden" or "Rejecting" verb and no
  stack trace.
- Enforce config-version scope at the profile, not the revision, and refuse the
  request when the scope lookup raises `ScopeLookupError`.
- Treat an S3 `AccessDenied` seen by a resolver as a server fault (500). Don't
  pass S3's message through to the caller.
- A new role touches several files, and none can be skipped:
  - the `AWS::Cognito::UserPoolGroup` in `template.yaml`
  - the schema directives
  - the expectations file, then a regenerated manifest
  - `VALID_PERSONAS` in `user_management`
  - `APP_GROUPS` in `use-user-role.ts`
  - the navigation

## Must not
- Hand-edit `http_api_dispatcher/api_rbac_manifest.json`. It is generated.
- Fix a 403 by widening an entry, adding a bypass, falling through on an unmapped
  field, or declaring the operation `ANY`.
- Expand `ANY_GROUP` into a list of group names in the expectations file. The
  schema directive, however, must list the groups.
- Use `@aws_auth(...)`. It is silently ignored on this API.
- Let a catch-all re-wrap a refusal as a generic `Exception`, because it then
  becomes a 500.
- Treat UI hiding as a security boundary, or describe an `ANY_GROUP` operation as
  making document content unreachable. Signed-in users can still read the
  document buckets directly.
- Read a SKIP or WARN result from `make api-test` as a pass.

## Verify with
- `make api-test-static`. Add `STRICT=1` to confirm a known gap is closed.
- `make api-test STACK_NAME=<stack>` against a deployed stack that has processed
  a document
- `lib/idp_common_pkg/tests/unit/test_http_api_dispatcher_authz.py`,
  `scripts/sdlc/tests/test_scan_api_rbac_function_urls.py`,
  `src/ui/src/hooks/__tests__/use-user-role.appGroups.test.ts`

## Sources
- [.claude/skills/api-rbac-test.md](../skills/api-rbac-test.md): the architecture
  under test, the sources of truth, the policy table, outcome vocabulary and
  triage order
- [.claude/skills/live-auth-checks.md](../skills/live-auth-checks.md): the
  pre-token group-mapping trigger and federation checks
- [docs/rbac.md](../../docs/rbac.md): enforcement layers, scope resolution, status
  mapping and known limitations
