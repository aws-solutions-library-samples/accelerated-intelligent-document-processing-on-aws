# Security Test Snapshot — 0.6.11

Auditable, public-safe summary of this release's security tests. Environment-specific identifiers (account IDs, pool IDs, API hostnames, request IDs, local paths) are redacted; raw reports live in gitignored `scratch/`/`.srt/` and are not published.

## Provenance

| Field | Value |
|-------|-------|
| Release version | `0.6.11` |
| Git SHA | `64e95c7d2` |
| Snapshot date | 2026-10-09 |
| Curated by | `scripts/security/curate_results.py` |

## Results

| Test | Gate | Detail |
|------|------|--------|
| [SRT — SAST & deps](./srt.md) | FAIL ❌ | 7 open/reopened HIGH of 34976 tracked |
| [RBAC — static](./rbac-static.md) | PASS ✅ | 0 fail, 4 known-gap warn |
| [RBAC — dynamic](./rbac-dynamic.md) | PASS ✅ | 609 checks, 0 hard fail, 0 not run |
| [ZAP DAST](./zap-dast.md) | PASS ✅ | High=0 |

See [`security/README.md`](../../README.md) for what each test covers and how to run it.
