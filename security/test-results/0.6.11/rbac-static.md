# RBAC — Static Authorization Scan

Offline cross-check (no AWS): reconciles the API op universe, the schema `@aws_cognito_user_pools` directives, and the expectations file (`scripts/api_rbac_expectations.yaml`) for drift and missing server-side checks. WARN entries are known/accepted authorization gaps (documented in the expectations file), not failures.

## Summary

- **Gate:** PASS ✅ (4 known-gap warnings)
- **API operations covered:** 119
- **Result:** 0 FAIL · 4 WARN (known gaps)

## Checks executed

The scan runs this fixed battery against every operation; the gate fails on any FAIL finding.

| Check | What it verifies | Outcome |
|-------|------------------|---------|
| **S0** Declaration integrity | every `groups:` value is a list of real Cognito group names or one of `ANY` / `ANY_GROUP` / `IAM_ONLY`, and every gap id is defined in the register — an unrecognised policy sentinel fails rather than being read by the later checks as the most permissive branch they have | PASS ✅ |
| **S1** Manifest completeness | every routable op has an expectations entry and every entry maps to a real op (no stale rows) | PASS ✅ |
| **S2** Schema ↔ expectations consistency | schema.graphql `@aws_cognito_user_pools` groups match expected groups (documented drift allowed via `schema_groups`/`known_gap`) | PASS ✅ |
| **S3** Resolver enforcement | each op's `enforced_in` source contains a recognized enforcement pattern (group check, ownership, or IAM-only rejection); `ANY`/`ANY_GROUP` ops without one must carry a known_gap or declare ownership, their group floor being the dispatcher's generated manifest rather than the resolver | PASS ✅ |
| **S4** Scope enforcement | ops flagged `scope_checked`/`scope_filtered` reference allowedConfigVersions in their `enforced_in` file | PASS ✅ |
| **S5** Template method auth | every API Gateway method is COGNITO_USER_POOLS except the allowlisted CORS (OPTIONS) and static-SPA (GET) routes | PASS ✅ |

## Captured output (known gaps + result)

```
=== Static API RBAC scan ===
  ⚠ [GAP] GAP-02: queryKnowledgeBase has no group check — affects: queryKnowledgeBase
  ⚠ [GAP] GAP-07: the chat Function URL transport carries no Cognito group claim, so neither chat route's group check can be applied to callers arriving that way
 — affects: POST /chat/agent, POST /chat/document
  ⚠ [S9] (POST /chat/document) route declares 'ANY_GROUP' but this transport can only enforce 'ANY', so a caller the declared policy would refuse is not refused here [GAP-07]
  ⚠ [S9] (POST /chat/agent) route declares ['Admin', 'Author', 'Viewer'] but this transport can only enforce 'ANY', so a caller the declared policy would refuse is not refused here [GAP-07]

0 FAIL, 4 WARN
```
