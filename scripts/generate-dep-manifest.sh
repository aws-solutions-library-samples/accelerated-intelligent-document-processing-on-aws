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
"""Collect every pinned Python dependency from lockfiles and requirements files."""
import subprocess
import sys
import tomllib
import re
from pathlib import Path
from packaging.version import InvalidVersion, Version

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


def repo_files(pattern: str) -> list[Path]:
    """Files matching ``pattern`` that the repository holds, not just the disk.

    Tracked files plus untracked files no ignore rule covers: a dependency file a
    developer has not yet ``git add``-ed is part of their change, but a gitignored
    copy (a staged build tree, a scratch worktree inside the checkout) never
    ships, and auditing it makes a local run fail on advisories CI never sees.
    A walk of the disk cannot tell those apart, so outside a git checkout this
    fails rather than guessing. Issue #1675.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard", "--", f":(glob)**/{pattern}"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        sys.exit("ERROR: `git ls-files` cannot run: git is not on PATH.")
    if proc.returncode != 0:
        sys.exit(
            f"ERROR: `git ls-files` failed in {repo_root}, so the dependency files "
            f"to audit cannot be told apart from gitignored copies:\n{proc.stderr}"
        )
    found = {repo_root / rel for rel in proc.stdout.split("\0") if rel}
    # A tracked file deleted in the working tree is still listed by --cached.
    return sorted(path for path in found if path.is_file())


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def is_internal(name: str) -> bool:
    return normalize(name) in INTERNAL_PACKAGES


# Strategy: parse existing uv.lock files for already-resolved packages, record
# every exact `==` pin in a requirements file, then collect the remaining names
# from pyproject.toml and requirements.txt for `uv pip compile` to resolve. The
# loose names are resolved by name alone, which avoids re-resolution conflicts.
#
# The manifest keeps every distinct (name, version) pair, not one version per
# name: a requirements file pinning `foo==1.5` installs 1.5 into its Lambda
# whatever version a lockfile carries, so keeping only the lockfile's version
# (or the highest of several) leaves the version that ships unaudited.

pinned: dict[tuple[str, str], str] = {}  # (normalized_name, version) -> "name==version"
locked: set[str] = set()  # normalized names some lockfile resolves

# An exact pin: a name, optional extras, `==` and a concrete version, optionally
# followed by an environment marker. `==1.*` and `==1.0,<2` are not exact.
EXACT_PIN = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*"
    r"([A-Za-z0-9][A-Za-z0-9.+!_-]*)\s*(?:;.*)?$"
)


def record(name: str, version: str) -> None:
    # Written normalized, because the shell merge below finds a name already in
    # the manifest by its normalized spelling; `Foo_Bar==1.0` written verbatim
    # would let the resolver's `foo-bar` be appended beside it as a phantom pair.
    key = normalize(name)
    pinned.setdefault((key, version), f"{key}=={version}")

# 1. Parse all existing uv.lock files (already resolved, no conflicts)
for lockfile in repo_files("uv.lock"):
    content = lockfile.read_text()
    for block in re.split(r"\n(?=\[\[package\]\])", content):
        name_m = re.search(r'^name\s*=\s*"(.+?)"', block, re.MULTILINE)
        ver_m = re.search(r'^version\s*=\s*"(.+?)"', block, re.MULTILINE)
        if name_m and ver_m:
            name = name_m.group(1)
            version = ver_m.group(1)
            if version == "0.0.0" or is_internal(name):
                continue
            record(name, version)
            locked.add(normalize(name))

# 2. Collect additional packages from pyproject.toml and requirements.txt
#    that no lockfile resolves
additional: set[str] = set()
# Normalized names that some file asks for without an exact pin. For these the
# resolver's version is roughly what that consumer installs, so the merge adds
# it even when an exact pin of the same name elsewhere is already listed.
loose: set[str] = set()


def extract_pkg_name(spec: str) -> str:
    clean = spec.split(";")[0].strip()
    return re.split(r"[>=<!\[;\s(~]", clean)[0].strip()


def is_path_ref(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith((".", "/", "../../"))


def consider(spec: str) -> None:
    """Record ``spec`` as a pin if it is exact; queue its name for the resolver."""
    if is_path_ref(spec):
        return
    pkg = extract_pkg_name(spec)
    if not pkg or is_internal(pkg):
        return
    pin = EXACT_PIN.match(spec)
    if pin:
        record(pin.group(1), pin.group(2))
    if normalize(pkg) in locked:
        return
    # A pinned name not in a lockfile still goes to the resolver, so its
    # transitive dependencies are found; the merge below skips the resolver's
    # version of the name itself unless some other file asks for it loosely.
    additional.add(pkg)
    if not pin:
        loose.add(normalize(pkg))


def requirement_lines(text: str) -> list[str]:
    """Logical requirement lines: continuations joined, comments and hashes dropped.

    `pip-compile --generate-hashes` writes `foo==1.0 \\` followed by indented
    `--hash=...` lines; read physically, the pin carries a trailing backslash and
    the hashes look like options, so the pin would not be recognised as exact.
    """
    out = []
    for line in re.sub(r"\\\r?\n", " ", text).splitlines():
        line = line.split("#")[0]
        line = " ".join(t for t in line.split() if not t.startswith("--hash"))
        if line and not line.startswith("-"):
            out.append(line)
    return out


# From pyproject.toml files
for toml_path in sorted(repo_root.glob("lib/*/pyproject.toml")):
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    project = data.get("project", {})
    for dep in project.get("dependencies", []):
        consider(dep)
    for group_deps in project.get("optional-dependencies", {}).values():
        for dep in group_deps:
            consider(dep)

# From requirements.txt files
for req_path in repo_files("requirements*.txt"):
    for line in requirement_lines(req_path.read_text()):
        consider(line)


def version_order(pair: tuple[str, str]) -> tuple:
    try:
        return (pair[0], 0, Version(pair[1]), "")
    except InvalidVersion:
        return (pair[0], 1, Version("0"), pair[1])


# Write pinned packages to output; write additional to a temp file for resolution
with open(output_file, "w") as f:
    for key in sorted(pinned, key=version_order):
        f.write(pinned[key] + "\n")

# Names asked for loosely, for the shell merge
loose_file = Path(sys.argv[2]).parent / ".loose-names.txt"
loose_file.write_text("".join(f"{name}\n" for name in sorted(loose)))

# Write additional packages to a temp file so the shell can resolve them
additional_file = Path(sys.argv[2]).parent / ".additional-reqs.txt"
if additional:
    with open(additional_file, "w") as f:
        for pkg in sorted(additional, key=str.lower):
            f.write(pkg + "\n")
    print(f"  {len(pinned)} pinned packages, {len(additional)} to resolve")
else:
    additional_file.unlink(missing_ok=True)
    print(f"  {len(pinned)} pinned packages, 0 additional")
    print(f"  -> {sys.argv[2]} ({len(pinned)} total)")
PYTHON_SCRIPT

# Resolve additional packages (not in lockfiles) with uv pip compile
ADDITIONAL_REQS="$OUTPUT_DIR/.additional-reqs.txt"
LOOSE_NAMES="$OUTPUT_DIR/.loose-names.txt"
if [ -f "$ADDITIONAL_REQS" ]; then
    echo "  Resolving additional packages with uv pip compile..."
    ADDITIONAL_RESOLVED="$OUTPUT_DIR/.additional-resolved.txt"
    if uv pip compile "$ADDITIONAL_REQS" \
        --python-version "$PYTHON_VERSION" \
        --output-file "$ADDITIONAL_RESOLVED" \
        --no-header \
        --no-annotate \
        --quiet 2>/dev/null; then
        # Merge. A resolved pair already listed is skipped. A name some file asks
        # for loosely is added at the resolved version, since that is roughly
        # what the loose consumer installs. Any other name already listed (from
        # a lockfile or an exact pin) is skipped: its listed versions are the
        # ones that ship. Everything else is a new transitive dependency.
        BEFORE=$(wc -l < "$OUTPUT_DIR/python-packages.txt" | tr -d ' ')
        while IFS= read -r line; do
            pkg_name=$(echo "$line" | cut -d'=' -f1 | tr '[:upper:]' '[:lower:]' | tr '_.' '-')
            pkg_version=${line#*==}
            if grep -qixF "${pkg_name}==${pkg_version}" "$OUTPUT_DIR/python-packages.txt"; then
                continue
            fi
            if grep -qxF "$pkg_name" "$LOOSE_NAMES" \
                || ! grep -qi "^${pkg_name}==" "$OUTPUT_DIR/python-packages.txt"; then
                echo "${pkg_name}==${pkg_version}" >> "$OUTPUT_DIR/python-packages.txt"
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
rm -f "$LOOSE_NAMES"

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
echo "  python-packages.txt: name==version, one line per pinned version"
echo "  node-packages.txt:   npm-compatible (name@version)"
