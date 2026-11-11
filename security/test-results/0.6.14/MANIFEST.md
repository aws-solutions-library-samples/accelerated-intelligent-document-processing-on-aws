# Security Test Snapshot — 0.6.14

Auditable, public-safe summary of this release's security tests. Environment-specific identifiers (account IDs, pool IDs, API hostnames, request IDs, local paths) are redacted; raw reports live in gitignored `scratch/`/`.srt/` and are not published.

## Provenance

| Field | Value |
|-------|-------|
| Release version | `0.6.14` |
| Git SHA | `ada0a9bc5` |
| Snapshot date | 2026-11-07 |
| Curated by | `scripts/security/curate_results.py` |

## Results

| Test | Gate | Detail |
|------|------|--------|
| [SRT — SAST & deps](./srt.md) | PASS ✅ | 0 open/reopened HIGH of 53423 tracked, 57 scoped out |
| [RBAC — static](./rbac-static.md) | PASS ✅ | 0 fail, 4 known-gap warn |
| [RBAC — dynamic](./rbac-dynamic.md) | PASS ✅ | 1204 checks, 0 hard fail, 0 not run |
| [ZAP DAST](./zap-dast.md) | PASS ✅ | High=0 |

See [`security/README.md`](../../README.md) for what each test covers and how to run it.
