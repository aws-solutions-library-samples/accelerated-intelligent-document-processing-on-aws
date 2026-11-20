# Solution Profile contract (vendored)

The files under [`v1/`](v1/) are **vendored copies** of version 1 of the Solution
Profile contract. The accelerator's profile in [`solution-profile/`](../) is written
against them. They are kept byte-for-byte as published and must not be edited here,
not even to reformat them. A change to the contract is made by its owners and then
copied in again whole.

## Provenance

| | |
|---|---|
| What | The Solution Profile contract, version 1: its README, JSON Schema and reference validator |
| Vendored | 2026-11-20, unchanged |
| Maintained | Outside this repository, by the contract's owners. Only they change it; this repository takes a new version by copying the files in again |
| Integrity | The sha256 of each file is pinned below and checked by `scripts/tests/test_solution_profile.py` |

## Files

| File | What it is |
|---|---|
| [`v1/README.md`](v1/README.md) | The contract: the files, their fields and the gap taxonomy |
| [`v1/solution-profile.schema.json`](v1/solution-profile.schema.json) | The exact rules, as JSON Schema 2020-12 |
| [`v1/validate_profile.py`](v1/validate_profile.py) | The reference validator |

Because they are vendored, the repository's Python lint and format gates do not read
`v1/validate_profile.py` (a scope entry in `ruff.toml`, registered in
`scripts/tests/gate_exemptions.json`). What keeps that exclusion honest is the
hash pin below: `scripts/tests/test_solution_profile.py` fails if any of the three
files differs from the digest recorded here, so the copy cannot drift into a locally
modified validator that nothing lints.

<!-- vendored-sha256:start -->
```text
efb5561fd72e1edbf7c3b2bb3bfabe2c1dd08e9877f60bb01b04510976fee327  v1/README.md
ea6286ebfe79261811c3ec8203bf3d08da6c87f8b8471efc02586895c971b158  v1/solution-profile.schema.json
fb2d437d50e2c6df675797a5fdb7d20babcc2700deee2f40c94b79815ecd08d4  v1/validate_profile.py
```
<!-- vendored-sha256:end -->

To update the copies after the contract changes, copy the three files over these,
run `sha256sum v1/README.md v1/solution-profile.schema.json v1/validate_profile.py`
from this directory, paste the result between the markers above, and run
`make solution-profile-check`.

The tarball published with each release does not contain this directory; it holds
the profile files, its skill and `build.json`. See
[docs/solution-profile.md](../../docs/solution-profile.md).
