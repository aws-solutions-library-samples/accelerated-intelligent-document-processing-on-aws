#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# The MR UX test's toolchain, defined once. The ai_ux_test CI job runs this as
# root from the target branch's copy, and scripts/ux_runner/Dockerfile runs it to
# build the local image, so CI and a laptop get the same Chrome, Node, npm pins,
# ffmpeg, fonts, Python pins, managed policy and users. Each download is checked
# before it is used: the Chrome for Testing zip and the Node tarball against the
# sha256 recorded below for this architecture, the Python packages against the
# digests requirements.txt records (pip --require-hashes), the npm packages
# against package-lock.json's integrity hashes, and apt's packages against its
# repositories' signatures. The one exception is npm's upgrade of itself to
# NPM_VERSION, which is checked only against what the npm registry publishes for
# that version. The job runs this with none of its secrets in the environment,
# which keeps them out of a package script's reach by accident, not from a
# hostile one: that runs as root beside the job's shell. The checks above are
# what guard against that.
#
# The layout it produces is the contract scripts/ux_runner/paths.py reads:
# <prefix>/chrome/chrome, <prefix>/node_modules/.bin/{claude,chrome-devtools-mcp}
# and <prefix>/VERSIONS.json, with the prefix from UX_RUNNER_PREFIX. Every
# variable here is UPPERCASE: scripts/tests/exemption_discovery.py reads
# lower-case shell assignments as possible gate exemptions.
set -euo pipefail
# Every file and directory this makes is created under this umask, whatever the
# caller's: a GitLab Docker executor runs a job at umask 0000 by default, and a
# group- or world-writable toolchain, site-packages or policy directory would let
# the browser's uid change what root runs next.
umask 022

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${UX_RUNNER_PREFIX:-/opt/ux-runner}"
NODE_VERSION="v22.14.0"            # = .gitlab-ci.yml .node_setup (pinned by a test)
NPM_VERSION="11.1.0"               # = .gitlab-ci.yml .node_setup (pinned by a test)
# The sha256 of node-<NODE_VERSION>-linux-<arch>.tar.xz for each architecture, from
# https://nodejs.org/dist/<NODE_VERSION>/SHASUMS256.txt. Moving NODE_VERSION, with
# .node_setup, means taking both of these again from that release's file.
NODE_SHA256_X64="69b09dba5c8dcb05c4e4273a4340db1005abeafe3927efda2bc5b249e80437ec"
NODE_SHA256_ARM64="08bfbf538bad0e8cbb0269f0173cca28d705874a67a22f60b57d99dc99e30050"
# Chrome for Testing: the current Stable that ships both linux64 and linux-arm64,
# and the sha256 of each platform's zip at that version. To move the pin, take the
# Stable version from
# https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json
# and `sha256sum` both chrome-<platform>.zip downloads of it.
CFT_VERSION="155.0.8059.84"
CFT_SHA256_LINUX64="cf0f049892dea22e6a6db26d47ed0d7776b7158edcbec4634086633df8bafcbc"
CFT_SHA256_LINUX_ARM64="1692a407908867de88eb1f1b2a93ca47c348d8fb2e3c04752d966e0f2e0344b4"
BROWSER_USER="uxbrowser"
BROWSER_UID="10001"
PROXY_USER="uxproxy"
PROXY_UID="10002"
RENDER_USER="uxrender"
RENDER_UID="10003"
POLICY_DIRS="/etc/opt/chrome_for_testing/policies/managed /etc/opt/chrome/policies/managed"
POLICY_NAME="ux-runner.json"

case "$(uname -m)" in
  x86_64)        NODE_ARCH=x64;   NODE_SHA256="$NODE_SHA256_X64";   CFT_PLATFORM=linux64;     CFT_SHA256="$CFT_SHA256_LINUX64" ;;
  aarch64|arm64) NODE_ARCH=arm64; NODE_SHA256="$NODE_SHA256_ARM64"; CFT_PLATFORM=linux-arm64; CFT_SHA256="$CFT_SHA256_LINUX_ARM64" ;;
  *) echo "❌ unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

# Preconditions, checked before anything is downloaded: a run that cannot finish
# should say why in its first lines rather than after the apt and Node installs.
if [ "$(id -u)" != "0" ]; then
  echo "❌ install.sh must run as root: it installs packages, writes the managed policy and creates users" >&2
  exit 1
fi
for FILE in package.json package-lock.json requirements.txt chrome_policy.json; do
  if [ ! -f "$HERE/$FILE" ]; then
    echo "❌ $HERE/$FILE is missing; install.sh reads it from its own directory" >&2
    exit 1
  fi
done
if ! [[ "$CFT_VERSION" =~ ^[0-9]+(\.[0-9]+){3}$ && "$CFT_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "❌ no Chrome for Testing pin is recorded for $CFT_PLATFORM (version '$CFT_VERSION')." >&2
  echo "   Record CFT_VERSION and both CFT_SHA256_* values at the top of $HERE/install.sh." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# The CI job starts this in the MR's checkout. `python3 -` and `python3 -m` put
# the working directory first on sys.path, and npm reads a project's .npmrc, so
# everything from here on runs in an empty directory of its own, and Python
# runs isolated (-I) on top: nothing the MR committed can shadow pip, the
# standard library or the registry.
cd "$WORK"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl git unzip xz-utils ffmpeg \
    fonts-dejavu-core fonts-noto-color-emoji fonts-liberation

# Node, the same tarball .node_setup uses, checked against the recorded digest
# before anything in it is extracted
if [ "$(node --version 2>/dev/null || true)" != "$NODE_VERSION" ]; then
  curl -fsSL "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" -o "$WORK/node.tar.xz"
  echo "${NODE_SHA256}  $WORK/node.tar.xz" | sha256sum -c -
  tar -xJf "$WORK/node.tar.xz" -C /usr/local --strip-components=1
  npm install -g "npm@${NPM_VERSION}"
fi
node --version

# Chrome for Testing, checked against the recorded digest
curl -fsSL "https://storage.googleapis.com/chrome-for-testing-public/${CFT_VERSION}/${CFT_PLATFORM}/chrome-${CFT_PLATFORM}.zip" -o "$WORK/chrome.zip"
echo "${CFT_SHA256}  $WORK/chrome.zip" | sha256sum -c -
unzip -q "$WORK/chrome.zip" -d "$WORK"
# deb.deps lines are Debian dependency strings with version floors, such as
# "libasound2 (>= 1.0.17)", which `apt-get install` cannot take as package names;
# `apt-get satisfy` takes them as they are, alternatives included.
if [ -f "$WORK/chrome-${CFT_PLATFORM}/deb.deps" ]; then
  DEPS="$(grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$WORK/chrome-${CFT_PLATFORM}/deb.deps" | paste -sd, -)"
else
  DEPS="libnss3, libatk-bridge2.0-0, libatk1.0-0, libcups2, libdrm2, libgbm1, libxkbcommon0, libxcomposite1, libxdamage1, libxfixes3, libxrandr2, libpango-1.0-0, libcairo2, libasound2, libatspi2.0-0"
fi
apt-get satisfy -y --no-install-recommends "$DEPS"
mkdir -p "$PREFIX" && rm -rf "$PREFIX/chrome" && mv "$WORK/chrome-${CFT_PLATFORM}" "$PREFIX/chrome"
"$PREFIX/chrome/chrome" --version

# npm pins, from the committed lockfile, which dep-audit reads (NODE_LOCKFILES)
cp "$HERE/package.json" "$HERE/package-lock.json" "$PREFIX/"
npm ci --prefix "$PREFIX" --omit=dev --no-audit --no-fund
"$PREFIX/node_modules/.bin/claude" --version
test -x "$PREFIX/node_modules/.bin/chrome-devtools-mcp"

# Python pins: exactly lib/idp_common_pkg/uv.lock's versions, which dep-audit
# checks, each with the sha256 of every file pip may take for it on this image, so
# pip refuses any other file; requirements.txt says which files those are
python3 -I -m pip install --no-cache-dir --no-deps --require-hashes --root-user-action=ignore -r "$HERE/requirements.txt"

# Managed policy, root's to write and everybody's to read
for DIR in $POLICY_DIRS; do
  install -d -m 0755 "$DIR"
  install -m 0644 "$HERE/chrome_policy.json" "$DIR/$POLICY_NAME"
done

# Unprivileged users: Chrome as one uid, the proxy as another, so the socket audit
# can attribute every socket of the browser uid to Chrome, and render as a third,
# so the process that decodes the page's frames cannot read root's environment.
# An existing user with a different uid would make the audit watch the wrong
# sockets, so it is refused.
id -u "$BROWSER_USER" >/dev/null 2>&1 || useradd --uid "$BROWSER_UID" --user-group --no-create-home --shell /usr/sbin/nologin "$BROWSER_USER"
id -u "$PROXY_USER"   >/dev/null 2>&1 || useradd --uid "$PROXY_UID"   --user-group --no-create-home --shell /usr/sbin/nologin "$PROXY_USER"
id -u "$RENDER_USER"  >/dev/null 2>&1 || useradd --uid "$RENDER_UID"  --user-group --no-create-home --shell /usr/sbin/nologin "$RENDER_USER"
if [ "$(id -u "$BROWSER_USER")" != "$BROWSER_UID" ] || [ "$(id -u "$PROXY_USER")" != "$PROXY_UID" ] || [ "$(id -u "$RENDER_USER")" != "$RENDER_UID" ]; then
  echo "❌ $BROWSER_USER, $PROXY_USER and $RENDER_USER must have uids $BROWSER_UID, $PROXY_UID and $RENDER_UID" >&2
  exit 1
fi

# VERSIONS.json (data_formats D-VERSIONS): read back from what was installed
# rather than restated from the pins above.
UX_PREFIX="$PREFIX" UX_CFT_VERSION="$CFT_VERSION" UX_CFT_PLATFORM="$CFT_PLATFORM" python3 -I - <<'PY'
import json
import os
import subprocess
from pathlib import Path

prefix = Path(os.environ["UX_PREFIX"])


def package_version(name):
    manifest = prefix / "node_modules" / name / "package.json"
    return json.loads(manifest.read_text(encoding="utf-8"))["version"]


def first_line(*argv):
    done = subprocess.run(argv, capture_output=True, text=True, check=True)
    return done.stdout.splitlines()[0].strip()


versions = {
    "chrome": os.environ["UX_CFT_VERSION"],
    "chrome_platform": os.environ["UX_CFT_PLATFORM"],
    "node": first_line("node", "--version"),
    "chrome_devtools_mcp": package_version("chrome-devtools-mcp"),
    "claude_code": package_version("@anthropic-ai/claude-code"),
    "ffmpeg": first_line("ffmpeg", "-version"),
}
target = prefix / "VERSIONS.json"
target.write_text(json.dumps(versions, indent=2) + "\n", encoding="utf-8")
print(f"{target}: {json.dumps(versions)}")
PY
