# PyPI name placeholders (defensive registration)

These are **not** real packages. Each is a minimal, deliberately non-functional
distribution published solely to **hold a first-party name on public PyPI** so a
third party cannot register it and turn a bare requirement in this repo into a
dependency-confusion vector.

## Background

Packages in `lib/` depend on their siblings by bare name (e.g. `idp_sdk`
requires `"idp_common"`). Those packages are first-party — they live in this
repo and are installed from the local checkout — but a bare requirement is
satisfiable from public PyPI whenever the sibling is not already installed. If
an attacker owns the name, pip installs their code.

Two of our names were already taken by the time we looked, so registration is no
longer available for those and the remedy is a name-reclamation request to PyPI.

The names in this directory were still unclaimed, so we hold them ourselves.
Prevention beats detection: the tripwire (`scripts/check_first_party_deps.py`)
catches a wrong package after the fact, but owning the name means there is
nothing wrong to install in the first place.

## Why these stubs fail loudly

Each placeholder raises `RuntimeError` on import with an explanation and a
pointer to the real install path.

This is deliberate. A stub that imports silently and exports nothing is exactly
the failure mode that cost us an afternoon of debugging: the squatted `idp_sdk`
imported fine, so `from idp_sdk import IDPClient` raised a confusing
`ImportError` far from the cause. If someone ever installs one of these
placeholders by accident, they should be told immediately and precisely.

## Do not depend on these

Nothing in this repo should ever install from these directories. They exist only
to be uploaded to PyPI. The real packages live in `lib/`:

| Placeholder name      | Real package                |
| --------------------- | --------------------------- |
| `idp-feature-sdk`     | `lib/idp_feature_sdk`       |
| `idp-mcp-connector`   | `lib/idp_mcp_connector_pkg` |
| `idp-accelerator-cli` | `lib/idp_cli_pkg`           |

`idp-accelerator-cli` is a slightly different case from the other two. It is not
a name we already used — it is the **new** distribution name for our CLI, adopted
because `idp-cli` on PyPI belongs to an unrelated legitimate project. We register
it here for the same reason as the others: so the name we now depend on cannot be
taken by someone else. The command users type is still `idp-cli`.

## Status

| Name                  | Uploaded      | Yanked        |
| --------------------- | ------------- | ------------- |
| `idp-feature-sdk`     | ✅ 2026-07-29 | ✅ 2026-07-30 |
| `idp-mcp-connector`   | ✅ 2026-07-29 | ✅ 2026-07-30 |
| `idp-accelerator-cli` | ✅ 2026-07-30 | ✅ 2026-07-30 |

Both columns must be ticked for a name to be fully handled — see why below. All
three are complete: the names are held by AWS, and a bare `pip install <name>`
finds no installable version (verified — pip reports "Could not find a version",
where an un-yanked placeholder would have resolved).

Nothing further is needed here unless a **new** first-party distribution name is
added, in which case follow the two steps below for it.

## Publishing

Only needs doing once per name. Two steps: upload, then yank.

### 1. Upload

Needs a PyPI API token (username `__token__`). `uvx` avoids installing twine into
your environment:

```bash
cd scripts/pypi-placeholders/<name>
python3 -m build
uvx twine check dist/*
uvx twine upload dist/*
```

Consider `uvx twine upload --repository testpypi dist/*` first to check the
metadata and README rendering — a filename can never be reused on PyPI, even
after deletion.

### 2. Yank the release — **do not skip this**

Each placeholder is version `0.0.0` *and* must be yanked. The version number
alone does not protect anything: an un-yanked `0.0.0` is still the only release,
so pip resolves it happily for a bare requirement:

```
$ pip install <name>                 # while un-yanked
Would install <name>-0.0.0           # ← resolves, defeating the purpose
```

Yanking ([PEP 592][pep592]) keeps the name reserved — nobody else can claim it —
while telling pip to ignore the release for any unpinned requirement. That is the
combination we want: name held, never installed by accident.

There is **no API for this**; `twine` supports only `check`, `upload` and
`register`. It must be done in the web UI:

1. Sign in to <https://pypi.org/manage/projects/>
2. Pick the project → **Manage** → **Releases**
3. On `0.0.0`, open **Options** → **Yank**
4. Reason: `Reserved-name placeholder — not a functional package`

Yanking is reversible and does **not** delete the release or free the name.

Verify afterwards:

```bash
curl -s https://pypi.org/pypi/<name>/json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
print([(f['filename'], f['yanked']) for fs in d['releases'].values() for f in fs])"
```

Both files should report `True`.

[pep592]: https://peps.python.org/pep-0592/
