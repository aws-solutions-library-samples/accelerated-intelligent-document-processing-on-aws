# Security Test Snapshot — 0.6.9

Auditable, public-safe summary of this release's security tests. Environment-specific identifiers (account IDs, pool IDs, API hostnames, request IDs, local paths) are redacted; raw reports live in gitignored `scratch/`/`.srt/` and are not published.

## Provenance

| Field | Value |
|-------|-------|
| Release version | `0.6.9` |
| Git SHA | `08b1ed242` |
| Snapshot date | 2026-09-19 |
| Curated by | `scripts/security/curate_results.py` |

## Results

| Test | Gate | Detail |
|------|------|--------|
| [SRT — SAST & deps](./srt.md) | PASS ✅ | 0 open/reopened HIGH of 18204 tracked |
| [RBAC — static](./rbac-static.md) | PASS ✅ | 0 fail, 2 known-gap warn |
| [RBAC — dynamic](./rbac-dynamic.md) | PASS ✅ | 604 checks, 0 hard fail |
| [ZAP DAST](./zap-dast.md) | PASS ✅ | High=0 |

See [`security/README.md`](../../README.md) for what each test covers and how to run it.
