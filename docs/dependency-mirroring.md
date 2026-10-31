---
title: "Dependency Mirroring for Air-Gapped Builds"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Dependency Mirroring for Air-Gapped Builds

Generate a complete list of all Python and Node.js dependencies so they can be mirrored into an artifact repository for air-gapped, pre-scanned builds.

## Quick Start

```bash
make dep-manifest
```

Output (gitignored, under `dist/manifests/`):
- `python-packages.txt` — one `name==version` per line; a package appears once per version the repository pins
- `node-packages.txt` — npm-compatible format (`name@version`)

## Prerequisites

| Tool | Purpose | Install |
|------|---------|---------|
| Python 3.12+ | Parses pyproject.toml files | System or pyenv |
| [uv](https://docs.astral.sh/uv/) | Resolves Python dependency tree | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [jq](https://jqlang.github.io/jq/) | Parses package-lock.json files | `brew install jq` / `apt install jq` |

## Output Format

**Python** (`python-packages.txt`):
```
boto3==1.42.0
jsonschema==4.25.1
pydantic==2.12.0
...
```

**Node** (`node-packages.txt`):
```
@aws-amplify/ui-react@6.7.1
react@18.3.1
vite@6.2.2
...
```

## CI Integration

The GitHub Actions workflow (`.github/workflows/generate-dep-manifest.yml`) runs automatically when dependency files change on `main`, or manually via `workflow_dispatch`.

Download manifests from the workflow run's **Artifacts** section (retained 90 days).

## Artifact Repository Setup

The manifests are designed to work with any artifact repository manager (JFrog Artifactory, AWS CodeArtifact, Sonatype Nexus, Azure Artifacts, etc.). Below are examples using a generic remote/virtual repository pattern.

### Python (PyPI)

1. Create a **remote repository** that proxies PyPI:
   - Package Type: PyPI
   - Upstream URL: `https://pypi.org`
   - (Optional) Set inclusion patterns from the manifest

2. Bulk-cache all packages, one line at a time:
   ```bash
   grep -v '^#' dist/manifests/python-packages.txt | while IFS= read -r pkg; do
       [ -n "$pkg" ] && pip download --no-deps --dest /tmp/cache \
           --index-url https://your-repo-manager/api/pypi/pypi-remote/simple "$pkg"
   done
   ```
   Do not pass the file to `pip install -r`. It lists every version the
   repository pins, so a package pinned at two versions (in a lockfile and in a
   Lambda's `requirements.txt`, say) appears twice, and pip refuses a
   requirements file naming two versions of one package.

3. Configure pip for air-gapped builds:
   ```ini
   # ~/.pip/pip.conf
   [global]
   index-url = https://your-repo-manager/api/pypi/pypi-virtual/simple
   trusted-host = your-repo-manager
   ```

### Node (npm)

1. Create a **remote repository** that proxies npmjs:
   - Package Type: npm
   - Upstream URL: `https://registry.npmjs.org`

2. Bulk-cache packages:
   ```bash
   while IFS= read -r pkg; do
       npm pack "$pkg" --registry=https://your-repo-manager/api/npm/npm-remote/ 2>/dev/null
   done < dist/manifests/node-packages.txt
   ```

3. Configure npm for air-gapped builds:
   ```ini
   # .npmrc
   registry=https://your-repo-manager/api/npm/npm-virtual/
   ```

### Known Exceptions

- `xlsx` in `src/ui/` uses a tarball URL (`https://cdn.sheetjs.com/...`), not the npm registry. Upload this manually to your repository's generic/raw storage.

## How It Works

The script (`scripts/generate-dep-manifest.sh`):

1. **Python**: Parses existing `uv.lock` files for already-resolved packages, records every exact `==` pin in a `requirements.txt` file at its pinned version, then scans `lib/*/pyproject.toml` and the `requirements.txt` files for any remaining packages not covered by the lockfiles and resolves those with `uv pip compile`. Every distinct name and version pair is kept, so a pin that differs from a lockfile's version is listed beside it rather than dropped. Internal packages and path references are filtered out. Lockfiles and requirements files are found with `git ls-files` (tracked files plus untracked files no ignore rule covers), so a gitignored copy such as a staged build tree is not read and a local run reads the same files CI does. The script therefore has to run inside a git checkout.

2. **Node**: Parses the committed `package-lock.json` files with `jq` to extract every resolved package and version. No install step needed.

Generated manifests are never committed — they live under `dist/` (gitignored) and are always regenerated fresh from the current dependency state.
