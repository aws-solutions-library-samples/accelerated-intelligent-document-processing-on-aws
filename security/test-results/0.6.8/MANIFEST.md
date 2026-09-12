# Security Test Snapshot — 0.6.8

Auditable, public-safe summary of this release's security tests. Environment-specific identifiers (account IDs, pool IDs, API hostnames, request IDs, local paths) are redacted; raw reports live in gitignored `scratch/`/`.srt/` and are not published.

## Provenance

| Field | Value |
|-------|-------|
| Release version | `0.6.8` |
| Git SHA | `d584958e0` |
| Snapshot date | 2026-09-11 |
| Curated by | `scripts/security/curate_results.py` |

## Results

| Test | Gate | Detail |
|------|------|--------|
| [SRT — SAST & deps](./srt.md) | PASS ✅ | 0 open/reopened HIGH of 16352 tracked |
| [RBAC — static](./rbac-static.md) | PASS ✅ | 0 fail, 1 known-gap warn |
| [RBAC — dynamic](./rbac-dynamic.md) | PASS ✅ | 604 checks, 0 hard fail |
| [ZAP DAST](./zap-dast.md) | PASS ✅ | High=0 |

See [`security/README.md`](../../README.md) for what each test covers and how to run it.
