# Security Test Snapshot — 0.6.5

Auditable, public-safe summary of this release's security tests. Environment-specific identifiers (account IDs, pool IDs, API hostnames, request IDs, local paths) are redacted; raw reports live in gitignored `scratch/`/`.srt/` and are not published.

## Provenance

| Field | Value |
|-------|-------|
| Release version | `0.6.5` |
| Git SHA | `720f3052a` |
| Snapshot date | 2026-08-24 |
| Curated by | `scripts/security/curate_results.py` |

## Results

| Test | Gate | Detail |
|------|------|--------|
| [SRT — SAST & deps](./srt.md) | PASS ✅ | 0 open/reopened HIGH of 11161 tracked |
| [RBAC — static](./rbac-static.md) | PASS ✅ | 0 fail, 2 known-gap warn |
| [RBAC — dynamic](./rbac-dynamic.md) | PASS ✅ | 556 checks, 0 hard fail |
| [ZAP DAST](./zap-dast.md) | PASS ✅ | High=0 |

See [`security/README.md`](../../README.md) for what each test covers and how to run it.
