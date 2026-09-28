# Security Test Snapshot — 0.6.11

Auditable, public-safe summary of this release's security tests. Environment-specific identifiers (account IDs, pool IDs, API hostnames, request IDs, local paths) are redacted; raw reports live in gitignored `scratch/`/`.srt/` and are not published.

## Provenance

| Field | Value |
|-------|-------|
| Release version | `0.6.11` |
| Git SHA | `3ca2dcc4d` |
| Snapshot date | 2026-09-27 |
| Curated by | `scripts/security/curate_results.py` |

## Results

| Test | Gate | Detail |
|------|------|--------|
| [SRT — SAST & deps](./srt.md) | FAIL ❌ | 5 open/reopened HIGH of 34329 tracked |
| [RBAC — static](./rbac-static.md) | PASS ✅ | 0 fail, 4 known-gap warn |
| [RBAC — dynamic](./rbac-dynamic.md) | FAIL ❌ | 603 checks, 8 hard fail, 25 not run |
| [ZAP DAST](./zap-dast.md) | PASS ✅ | High=0 |

See [`security/README.md`](../../README.md) for what each test covers and how to run it.
