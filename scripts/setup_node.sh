#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Install the Node side of a development environment: src/ui and docs-site
# dependencies from their lockfiles, plus basedpyright for `make typecheck`.
#
# Usage: scripts/setup_node.sh auto|required
#
#   required  `make setup-node`. Fails if Node is missing or too old, or if any
#             install fails.
#   auto      the last step of `make setup` and `make setup-venv`. Those targets are
#             also how deploy-only users install idp-cli, and deploying needs no
#             Node (the web UI is built in CodeBuild), so here a missing or old
#             Node, or a failed install, prints a warning naming what was not
#             installed and exits 0. It does nothing in CI (CI or CODEBUILD_BUILD_ID
#             set) or with SKIP_NODE_SETUP=1, because every CI job installs the Node
#             dependencies it needs itself and CodeBuild runs `make setup`.
#
# Why this exists: `make fastlint` runs two gates out of src/ui/node_modules, and
# nothing in `make setup` installed it, so a checkout that had installed UI
# dependencies before they gained a package failed those gates with an ESLint
# stack trace.
set -u

MODE=${1:-required}
case "$MODE" in
auto | required) ;;
*)
	echo "usage: $0 auto|required" >&2
	exit 2
	;;
esac

REPO_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
MIN_NODE=22.12.0

if [ -t 1 ]; then
	YELLOW='\033[1;33m' RED='\033[0;31m' GREEN='\033[0;32m' NC='\033[0m'
else
	YELLOW='' RED='' GREEN='' NC=''
fi

warn() { printf "${YELLOW}%s${NC}\n" "$*"; }

if [ "$MODE" = auto ]; then
	if [ -n "${SKIP_NODE_SETUP:-}" ]; then
		echo "Node dependencies: not installed (SKIP_NODE_SETUP is set)."
		exit 0
	fi
	if [ -n "${CI:-}" ] || [ -n "${CODEBUILD_BUILD_ID:-}" ]; then
		echo "Node dependencies: not installed (CI installs its own)."
		exit 0
	fi
fi

# A failure here: fatal in required mode, a warning that names the remedy in auto.
give_up() {
	if [ "$MODE" = required ]; then
		printf "${RED}ERROR: %s${NC}\n" "$1" >&2
		exit 1
	fi
	warn "⚠️  Node dependencies NOT installed: $1"
	warn "   Deploying and idp-cli do not need them. Lint, typecheck and the UI tests do:"
	warn "   fix the above, then run 'make setup-node'."
	exit 0
}

# True when version $1 >= version $2 (dotted numeric, no prerelease suffix).
version_at_least() {
	printf '%s\n%s\n' "$2" "$1" | sort -t. -k1,1n -k2,2n -k3,3n -C
}

command -v node >/dev/null 2>&1 || give_up "node is not on PATH (Node $MIN_NODE or later is needed)."
command -v npm >/dev/null 2>&1 || give_up "npm is not on PATH."
NODE_VERSION=$(node --version 2>/dev/null | sed 's/^v//')
version_at_least "$NODE_VERSION" "$MIN_NODE" ||
	give_up "Node $NODE_VERSION is older than the $MIN_NODE that src/ui/package.json requires."

echo "Installing Node dependencies with Node $NODE_VERSION, npm $(npm --version)..."

FAILED=""

echo "  src/ui (npm ci, from package-lock.json)..."
# Through the same lock as every UI make target, so this cannot rebuild
# node_modules under a gate that is reading it (issue #1447).
"$REPO_ROOT/scripts/ui_deps_lock.sh" 'npm ci --prefer-offline --no-audit --no-fund --loglevel=error' ||
	FAILED="$FAILED src/ui"

echo "  docs-site (npm ci, from package-lock.json)..."
(cd "$REPO_ROOT/docs-site" && npm ci --prefer-offline --no-audit --no-fund --loglevel=error) ||
	FAILED="$FAILED docs-site"

if command -v basedpyright >/dev/null 2>&1; then
	echo "  basedpyright: already on PATH ($(command -v basedpyright))."
else
	# The same command both CI configurations run.
	echo "  basedpyright (npm install -g basedpyright)..."
	npm install -g basedpyright --no-audit --no-fund --loglevel=error ||
		FAILED="$FAILED basedpyright(npm-install-g)"
fi

if [ -n "$FAILED" ]; then
	give_up "install failed for:$FAILED (see the npm output above)."
fi

printf "${GREEN}✅ Node dependencies installed: src/ui, docs-site, basedpyright.${NC}\n"
