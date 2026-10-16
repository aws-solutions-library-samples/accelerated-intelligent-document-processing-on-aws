#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="${REPO_ROOT}/dist/manifests"
PYTHON_VERSION="3.12"

mkdir -p "$OUTPUT_DIR"

# Pick a Python runner that has the `packaging` library available.
# Prefer `uv run` (installs `packaging` on the fly into an ephemeral env),
# which is what CI provisions; fall back to a system python3 that already
# has `packaging` importable.
if command -v uv >/dev/null 2>&1; then
    PY_RUN=(uv run --python "$PYTHON_VERSION" --with packaging python3)
elif python3 -c "import packaging" >/dev/null 2>&1; then
    PY_RUN=(python3)
else
    echo "ERROR: need either 'uv' (recommended) or a python3 with the 'packaging' library installed." >&2
    echo "       Install uv (https://docs.astral.sh/uv/) or run: pip install packaging" >&2
    exit 1
fi

# ===== PYTHON MANIFEST =====
echo "Generating Python dependency manifest..."

"${PY_RUN[@]}" - "$REPO_ROOT" "$OUTPUT_DIR/python-packages.txt" << 'PYTHON_SCRIPT'
"""Collect all Python dependencies from lockfiles and requirements, deduplicate by name."""
import sys
import tomllib
import re
from pathlib import Path
from packaging.version import Version

repo_root = Path(sys.argv[1])
output_file = Path(sys.argv[2])

# First-party packages that live in lib/ and are NOT published to PyPI. They must
# never appear in a mirroring manifest: the manifest is fetched from a public
# index, and several of these names are squatted by third parties on PyPI.
# Keep in sync with FIRST_PARTY in scripts/check_first_party_deps.py.
# See docs/dependency-confusion.md.
INTERNAL_PACKAGES = {
    "idp-common", "idp-sdk", "idp-accelerator-cli", "idp-mcp-connector",
    "idp-feature-sdk",
}

SKIP_DIRS = {".aws-sam", ".venv", "node_modules", ".git", "deps"}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def is_internal(name: str) -> bool:
    return normalize(name) in INTERNAL_PACKAGES


# Strategy: parse existing uv.lock files for already-resolved packages,
# then collect any additional packages from requirements.txt that aren't
# covered by the lockfiles. This avoids re-resolution conflicts.

resolved: dict[str, str] = {}  # normalized_name -> "name==version"

# 1. Parse all existing uv.lock files (already resolved, no conflicts)
for lockfile in sorted(repo_root.rglob("uv.lock")):
    rel = str(lockfile.relative_to(repo_root))
    if any(skip in rel for skip in SKIP_DIRS):
        continue
    content = lockfile.read_text()
    for block in re.split(r"\n(?=\[\[package\]\])", content):
        name_m = re.search(r'^name\s*=\s*"(.+?)"', block, re.MULTILINE)
        ver_m = re.search(r'^version\s*=\s*"(.+?)"', block, re.MULTILINE)
        if name_m and ver_m:
            name = name_m.group(1)
            version = ver_m.group(1)
            if version == "0.0.0" or is_internal(name):
                continue
            key = normalize(name)
            if key not in resolved or Version(version) > Version(resolved[key].split("==")[1]):
                resolved[key] = f"{name}=={version}"

# 2. Collect additional packages from pyproject.toml and requirements.txt
#    that aren't already in the resolved set
additional: set[str] = set()


def extract_pkg_name(spec: str) -> str:
    clean = spec.split(";")[0].strip()
    return re.split(r"[>=<!\[;\s(~]", clean)[0].strip()


def is_path_ref(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith((".", "/", "../../"))


# From pyproject.toml files
for toml_path in sorted(repo_root.glob("lib/*/pyproject.toml")):
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    project = data.get("project", {})
    for dep in project.get("dependencies", []):
        pkg = extract_pkg_name(dep)
        if not is_internal(pkg) and normalize(pkg) not in resolved:
            additional.add(pkg)
    for group_deps in project.get("optional-dependencies", {}).values():
        for dep in group_deps:
            if is_path_ref(dep):
                continue
            pkg = extract_pkg_name(dep)
            if not is_internal(pkg) and normalize(pkg) not in resolved:
                additional.add(pkg)

# From requirements.txt files
for req_path in sorted(repo_root.rglob("requirements*.txt")):
    rel = str(req_path.relative_to(repo_root))
    if any(skip in rel for skip in SKIP_DIRS):
        continue
    with open(req_path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line or line.startswith("-"):
                continue
            if is_path_ref(line):
                continue
            pkg = extract_pkg_name(line)
            if not is_internal(pkg) and normalize(pkg) not in resolved:
                additional.add(pkg)

# Write resolved packages to output; write additional to a temp file for resolution
with open(output_file, "w") as f:
    for key in sorted(resolved.keys()):
        f.write(resolved[key] + "\n")

# Write additional packages to a temp file so the shell can resolve them
additional_file = Path(sys.argv[2]).parent / ".additional-reqs.txt"
if additional:
    with open(additional_file, "w") as f:
        for pkg in sorted(additional, key=str.lower):
            f.write(pkg + "\n")
    print(f"  {len(resolved)} packages from lockfiles, {len(additional)} to resolve")
else:
    additional_file.unlink(missing_ok=True)
    print(f"  {len(resolved)} packages from lockfiles, 0 additional")
    print(f"  -> {sys.argv[2]} ({len(resolved)} total)")
PYTHON_SCRIPT

# Resolve additional packages (not in lockfiles) with uv pip compile
ADDITIONAL_REQS="$OUTPUT_DIR/.additional-reqs.txt"
if [ -f "$ADDITIONAL_REQS" ]; then
    echo "  Resolving additional packages with uv pip compile..."
    ADDITIONAL_RESOLVED="$OUTPUT_DIR/.additional-resolved.txt"
    if uv pip compile "$ADDITIONAL_REQS" \
        --python-version "$PYTHON_VERSION" \
        --output-file "$ADDITIONAL_RESOLVED" \
        --no-header \
        --no-annotate \
        --quiet 2>/dev/null; then
        # Merge: add only packages not already in the manifest
        BEFORE=$(wc -l < "$OUTPUT_DIR/python-packages.txt" | tr -d ' ')
        while IFS= read -r line; do
            pkg_name=$(echo "$line" | cut -d'=' -f1 | tr '[:upper:]' '[:lower:]' | tr '_.' '-')
            if ! grep -qi "^${pkg_name}==" "$OUTPUT_DIR/python-packages.txt" 2>/dev/null; then
                echo "$line" >> "$OUTPUT_DIR/python-packages.txt"
            fi
        done < "$ADDITIONAL_RESOLVED"
        AFTER=$(wc -l < "$OUTPUT_DIR/python-packages.txt" | tr -d ' ')
        echo "  Resolved $(( AFTER - BEFORE )) new transitive dependencies"
        rm -f "$ADDITIONAL_RESOLVED"
    else
        echo "  WARNING: uv pip compile failed for additional packages. Listing without versions:" >&2
        echo "" >> "$OUTPUT_DIR/python-packages.txt"
        echo "# Additional packages (resolution failed — install these to discover transitives)" >> "$OUTPUT_DIR/python-packages.txt"
        cat "$ADDITIONAL_REQS" >> "$OUTPUT_DIR/python-packages.txt"
    fi
    rm -f "$ADDITIONAL_REQS"
fi

PYTHON_COUNT=$(wc -l < "$OUTPUT_DIR/python-packages.txt" | tr -d ' ')
echo "  -> $OUTPUT_DIR/python-packages.txt ($PYTHON_COUNT packages)"

# ===== NODE MANIFEST =====
echo "Generating Node dependency manifest..."

NODE_LOCKFILES=(
    "$REPO_ROOT/src/ui/package-lock.json"
    "$REPO_ROOT/docs-site/package-lock.json"
    "$REPO_ROOT/scripts/ux_runner/package-lock.json"
)

{
    for lockfile in "${NODE_LOCKFILES[@]}"; do
        if [ -f "$lockfile" ]; then
            jq -r '
                .packages | to_entries[]
                | select(.key != "")
                | (.key | split("node_modules/") | last) + "@" + .value.version
            ' "$lockfile"
        else
            echo "  [skipped] $lockfile (not found)" >&2
        fi
    done
} | sort -u > "$OUTPUT_DIR/node-packages.txt"

NODE_COUNT=$(wc -l < "$OUTPUT_DIR/node-packages.txt" | tr -d ' ')
echo "  -> $OUTPUT_DIR/node-packages.txt ($NODE_COUNT packages)"

echo ""
echo "Done. Manifests at: $OUTPUT_DIR/"
echo "  python-packages.txt: pip-compatible (name==version)"
echo "  node-packages.txt:   npm-compatible (name@version)"
