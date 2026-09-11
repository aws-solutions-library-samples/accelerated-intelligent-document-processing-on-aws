# Security Test Snapshot — 0.6.5-verify

Auditable, public-safe summary of this release's security tests. Environment-specific identifiers (account IDs, pool IDs, API hostnames, request IDs, local paths) are redacted; raw reports live in gitignored `scratch/`/`.srt/` and are not published.

## Provenance

| Field | Value |
|-------|-------|
| Release version | `0.6.5-verify` |
| Git SHA | `e428d2ebb` |
| Snapshot date | 2026-08-25 |
| Curated by | `scripts/security/curate_results.py` |

## Results

| Test | Gate | Detail |
|------|------|--------|
| [SRT — SAST & deps](./srt.md) | PASS ✅ | 0 open/reopened HIGH of 11277 tracked |
| [RBAC — static](./rbac-static.md) | PASS ✅ | 0 fail, 2 known-gap warn |
| [RBAC — dynamic](./rbac-dynamic.md) | PASS ✅ | 556 checks, 0 hard fail |
| [ZAP DAST](./zap-dast.md) | PASS ✅ | High=0 |

See [`security/README.md`](../../README.md) for what each test covers and how to run it.
