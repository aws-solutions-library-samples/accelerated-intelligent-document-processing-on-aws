---
title: "Installing First-Party Packages Safely"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Installing First-Party Packages Safely

This project ships several **first-party** Python packages that live in `lib/`
(`idp_common`, `idp_sdk`, the CLI, the Feature Platform SDK, the MCP connector).
They are **not published to PyPI** and must always be installed from a local
checkout.

Some of those names exist on public PyPI, owned by unrelated parties. So
installing one by bare name — `pip install idp_common` — does not fail; it
installs **someone else's package** into the environment that holds your AWS
deployment credentials, and the mismatch usually surfaces later as a confusing
unrelated error rather than an obvious one.

Use `make setup` and this cannot happen. If you write the `pip install` yourself,
the next section is the whole of what you need to get right.

## Installing correctly

The normal path installs every first-party package in one pass:

```bash
make setup          # into the current environment
make setup-venv     # create a .venv and install into it
```

Two rules make any hand-written `pip install` safe, and they have to be applied
together:

1. **Every requirement is a path from the repository root**, never a bare name.
2. **Every first-party package a requirement needs is on the same command line.**
   The first-party packages require each other *by name* — `lib/idp_cli_pkg`
   requires `idp-sdk`, `lib/idp_sdk` requires `idp_common` — so installing them one
   at a time lets pip go looking for a sibling that is not on disk yet and fall back
   to the index. Rule 1 alone does not prevent that: `pip install -e lib/idp_sdk` on
   its own is a path install that still resolves `idp_common` from PyPI.

Which siblings a package needs is recorded in its `pyproject.toml`, next to the
bare requirement it explains. Applying both rules gives one command per component:

```bash
# idp_common has no first-party requirements, so it stands alone
pip install -e "lib/idp_common_pkg[extraction]"

# the SDK requires idp_common; the CLI requires the SDK, which requires idp_common
pip install -e lib/idp_common_pkg -e lib/idp_sdk
pip install -e lib/idp_common_pkg -e lib/idp_sdk -e lib/idp_cli_pkg

# the Feature Platform SDK and the MCP connector have no first-party requirements
pip install -e lib/idp_feature_sdk
pip install -e lib/idp_mcp_connector_pkg
```

`--no-deps` is the other way to be safe, because pip then resolves nothing at all —
but the siblings still have to get installed somehow, so it is only useful when you
know they are already there.

Lambda `requirements.txt` files already use relative paths, which are unaffected:

```
../../lib/idp_common_pkg[extraction]
```

## The two controls

There are two gates, and they cover different halves of the problem. Neither can
do the other's job.

### An environment that is already installed

`scripts/check_first_party_deps.py` checks that every installed first-party package
came from source rather than from a package index. It reads the
[PEP 610](https://peps.python.org/pep-0610/) `direct_url.json` that pip writes for
a local or VCS install and omits for an index install, so it works for editable and
non-editable installs alike:

```bash
python scripts/check_first_party_deps.py
```

Exit code 0 means everything resolved locally. `make setup` runs it automatically,
both CI systems run it on every pull request, and it is worth running yourself after
any manual `pip install` in a development environment.

Because it inspects an environment, it can only report on an install that has
already happened — which in CI is CI's own, correct install. It cannot see an
instruction in a document that nobody has followed yet.

### An install command that a document tells you to run

`scripts/tests/test_doc_install_commands.py` closes that gap. It reads every
`pip install` a reader could copy — a fenced code block in any tracked Markdown
file, and a code cell or markdown fence in any tracked notebook — and fails if any
of them could resolve a first-party name from an index, either because a requirement
is a bare name or because a package is installed from a path without the siblings it
requires by name. It runs on every pull request in both CI systems as part of
`make test-packages-cicd`:

```bash
pytest scripts/tests/test_doc_install_commands.py
```

The package names, and which siblings each one needs, are derived from the
`pyproject.toml` files at test time rather than listed in the test, so a new
first-party package with a bare sibling requirement is covered the moment it is
added.

What it does not read is prose — a command written in inline backticks while being
discussed, rather than in a code block to be copied. Nor does it match a command
that is not in a shell command position on its line: a parenthesised subshell, a
`sudo` carrying its own options, or a command assembled by a loop or held in a
variable. Installers other than pip (`uv add`, `poetry add`, `pipx`) resolve names
the same way and are also unmatched; `uv pip`, which this repository does use, is
matched. The module docstring states each of these and why, and they are the whole
list — nothing is excluded by path, so there is no exemption list to audit.

Being run is not the same as being enforced. Neither `develop` nor `main` carries
branch protection, so both gates above — and every other gate in this repository —
are advisory: a pull request can be merged with them red. `make
check-branch-protection` reads the live setting, and
[issue #933](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/933)
tracks turning protection on.

## If the environment check fails

1. **See what is installed.** A first-party package whose version does not match
   the repository's is the tell:

   ```bash
   pip list | grep -i idp
   ```

2. **Clean up and reinstall in one pass:**

   ```bash
   pip uninstall -y idp_common idp-sdk idp-accelerator-cli idp_feature_sdk \
       idp_mcp_connector
   make setup
   ```

3. **Then re-run the check** to confirm the environment is clean.

If a package from an index was installed in an environment holding AWS
credentials, treat it as you would any untrusted code execution: inspect the
artifact you actually received (`pip download` it and read it *without*
installing), and rotate credentials if you cannot rule out that it ran.

## A note on package names

Four identifiers are easy to conflate, and only the first affects installation:

| Identifier | Example |
| --- | --- |
| **Distribution name** — what `pip install` resolves | `idp-accelerator-cli` |
| Import name — what Python sees | `import idp_cli` |
| Console command — what you type | `idp-cli` |
| Runtime string literals — S3 prefixes, resource tags | `"idp-cli"` |

A distribution can be renamed without changing any of the others. The CLI's
distribution is `idp-accelerator-cli` because `idp-cli` on PyPI belongs to an
unrelated project; the command you type is still `idp-cli`.

## Related

- [Dependency Mirroring for Air-Gapped Builds](dependency-mirroring.md) — mirror
  dependencies into an internal artifact repository, which removes public-index
  resolution from your builds entirely.
- `scripts/pypi-placeholders/README.md` — names we hold on PyPI so they cannot be
  claimed by others.
