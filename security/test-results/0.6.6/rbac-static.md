# RBAC — Static Authorization Scan

Offline cross-check (no AWS): reconciles the API op universe, the schema `@aws_cognito_user_pools` directives, and the expectations file (`scripts/api_rbac_expectations.yaml`) for drift and missing server-side checks. WARN entries are known/accepted authorization gaps (documented in the expectations file), not failures.

## Summary

- **Gate:** PASS ✅ (2 known-gap warnings)
- **API operations covered:** 109
- **Result:** 0 FAIL · 2 WARN (known gaps)

## Checks executed

The scan runs this fixed battery against every operation; the gate fails on any FAIL finding.

| Check | What it verifies | Outcome |
|-------|------------------|---------|
| **S1** Manifest completeness | every routable op has an expectations entry and every entry maps to a real op (no stale rows) | PASS ✅ |
| **S2** Schema ↔ expectations consistency | schema.graphql `@aws_cognito_user_pools` groups match expected groups (documented drift allowed via `schema_groups`/`known_gap`) | PASS ✅ |
| **S3** Resolver enforcement | each op's `enforced_in` source contains a recognized enforcement pattern (group check, ownership, or IAM-only rejection); ANY-auth ops without one must carry a known_gap | PASS ✅ |
| **S4** Scope enforcement | ops flagged `scope_checked`/`scope_filtered` reference allowedConfigVersions in their `enforced_in` file | PASS ✅ |
| **S5** Template method auth | every API Gateway method is COGNITO_USER_POOLS except the allowlisted CORS (OPTIONS) and static-SPA (GET) routes | PASS ✅ |

## Captured output (known gaps + result)

```
=== Static API RBAC scan ===
  ⚠ [GAP] GAP-01: getStepFunctionExecution has no group or ownership check — affects: getStepFunctionExecution
  ⚠ [GAP] GAP-02: queryKnowledgeBase has no group check — affects: queryKnowledgeBase

0 FAIL, 2 WARN
```
