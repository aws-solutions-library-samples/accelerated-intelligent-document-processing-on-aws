# RBAC — Static Authorization Scan

Offline cross-check (no AWS): reconciles the API op universe, the schema `@aws_cognito_user_pools` directives, and the expectations file (`scripts/api_rbac_expectations.yaml`) for drift and missing server-side checks. WARN entries are known/accepted authorization gaps (documented in the expectations file), not failures.

## Summary

- **Gate:** PASS ✅ (2 known-gap warnings)
- **API operations covered:** 189
- **Result:** 0 FAIL · 2 WARN (known gaps)

## Checks executed

The scan runs this fixed battery against every operation; the gate fails on any FAIL finding.

| Check | What it verifies | Outcome |
|-------|------------------|---------|
| **S0** Declaration integrity | every `groups:` value is a list of real Cognito group names or one of `ANY` / `ANY_GROUP` / `IAM_ONLY`, and every gap id is defined in the register — an unrecognised policy sentinel fails rather than being read by the later checks as the most permissive branch they have | PASS ✅ |
| **S1** Manifest completeness | every routable op has an expectations entry and every entry maps to a real op (no stale rows) | PASS ✅ |
| **S2** Schema ↔ expectations consistency | schema.graphql `@aws_cognito_user_pools` groups match expected groups (documented drift allowed via `schema_groups`/`known_gap`) | PASS ✅ |
| **S3** Resolver enforcement | each op's `enforced_in` source contains a recognized enforcement pattern (group check, ownership, or IAM-only rejection); `ANY`/`ANY_GROUP` ops without one must carry a known_gap or declare ownership, their group floor being the dispatcher's generated manifest rather than the resolver | PASS ✅ |
| **S4** Scope enforcement | ops flagged `scope_checked`/`scope_filtered` reference the caller's config-version scope in the code the operation reaches, and `scope_filtered` ops apply a per-item scope predicate (bottoming out at `scope_allows`) to what they return | PASS ✅ |
| **S5** Template method auth | every API Gateway method is COGNITO_USER_POOLS except the allowlisted CORS (OPTIONS) and static-SPA (GET) routes | PASS ✅ |

## Captured output (known gaps + result)

```
=== Static API RBAC scan ===
  ⚠ [GAP] GAP-02: queryKnowledgeBase has no group check — affects: queryKnowledgeBase
  ⚠ [S10] (queryKnowledgeBase) takes session argument(s) ['sessionId'] but declares none of ['ownership', 'scope_checked']: say how the caller is confined to the session it names (and make the resolver do it), or record a known_gap if it is not [GAP-02]

0 FAIL, 2 WARN
```
