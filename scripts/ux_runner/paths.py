# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Where the UX runner's toolchain lives, and the fixed contract around it.

One module answers "where is Chrome, where is ``claude``, which port, which uid",
because four separately written artifacts have to agree on those answers and
nothing at run time can reconcile them if they do not:

* ``scripts/ux_runner/install.sh`` installs the toolchain into this layout under
  :data:`DEFAULT_PREFIX` and creates :data:`BROWSER_USER`, :data:`PROXY_USER` and
  :data:`RENDER_USER` with exactly these uids;
* ``scripts/ux_runner/chrome_policy.json`` names the proxy as
  ``http://127.0.0.1:3128``. The managed policy is static JSON installed with the
  toolchain, so the proxy port is a constant rather than something chosen per run;
* ``scripts/ux_test_proxy.py`` listens on ``PROXY_HOST:PROXY_PORT`` by default and
  serves the MR's build at :data:`UI_ORIGIN`;
* the socket audit attributes every socket owned by :data:`BROWSER_UID` to
  Chrome, which is only true while the proxy runs as a different uid.

Tests in the browser, toolchain and integration units pin each of those
agreements against the values here, so these are a contract: change one and the
pins say which artifact must change with it.

What this is NOT
----------------
It is not discovery. Nothing here searches ``PATH`` or probes for a Chrome that
happens to be installed: the unattended test runs exactly the pinned binaries or
reports them missing (:meth:`Toolchain.missing`), because a browser the harness
did not install is a browser the managed policy and the recorded versions say
nothing about.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: ``scripts/ux_runner/``.
PACKAGE_DIR = Path(__file__).resolve().parent
#: ``scripts/``, the directory callers put on ``sys.path``.
SCRIPTS_DIR = PACKAGE_DIR.parent
#: The root of the harness tree: the repository checkout, or the pinned
#: ``git archive`` extraction the CI job runs from.
HARNESS_ROOT = PACKAGE_DIR.parents[1]

#: Where ``install.sh`` puts the toolchain unless :data:`PREFIX_ENV` says otherwise.
DEFAULT_PREFIX = Path("/opt/ux-runner")
#: Environment variable that relocates the toolchain, read by ``install.sh`` too.
PREFIX_ENV = "UX_RUNNER_PREFIX"

#: Chrome's DevTools endpoint. Loopback only: the port is unauthenticated.
CDP_HOST = "127.0.0.1"
CDP_PORT = 9222

#: The egress proxy, which is the confined browser's only network path.
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 3128

#: The origin the proxy serves the MR's static build at.
UI_HOST = "localhost"
UI_PORT = 3000
UI_ORIGIN = "http://localhost:3000"

#: The page's viewport, in CSS pixels at a device pixel ratio of 1. The recorder
#: places click markers in these coordinates. Chrome's window is taller than this
#: by its own frame (``chrome.HEADLESS_FRAME_HEIGHT``).
VIEWPORT: tuple[int, int] = (1600, 1000)

#: Chrome runs as this user, and only Chrome does.
BROWSER_USER = "uxbrowser"
BROWSER_UID = 10001
#: The proxy runs as this user, so its upstream sockets are not Chrome's.
PROXY_USER = "uxproxy"
PROXY_UID = 10002
#: Render runs as this user, after Chrome has exited, so the process that decodes
#: the page's frames cannot read the orchestrator's environment through ``/proc``.
RENDER_USER = "uxrender"
RENDER_UID = 10003

#: The managed policy, the single source of truth for what is installed.
POLICY_FILE = PACKAGE_DIR / "chrome_policy.json"
#: Where ``install.sh`` copies the policy: the Chrome for Testing path, then the
#: branded Chrome one. Both are written because which of the two a Chrome for
#: Testing build reads on Linux has not been measured. The run refuses to start
#: unless an installed copy equals :data:`POLICY_FILE` and Chrome demonstrably
#: enforces it.
POLICY_INSTALL_DIRS: tuple[Path, ...] = (
    Path("/etc/opt/chrome_for_testing/policies/managed"),
    Path("/etc/opt/chrome/policies/managed"),
)
POLICY_INSTALL_NAME = "ux-runner.json"


@dataclass(frozen=True)
class Toolchain:
    """The pinned toolchain under one prefix.

    Every path is derived from :attr:`prefix` on access rather than stored, so a
    test can point one at ``tmp_path`` and a developer at a local Chrome for
    Testing install with ``--toolchain-prefix`` and get the same layout
    ``install.sh`` produces.
    """

    prefix: Path

    @property
    def chrome(self) -> Path:
        """The Chrome for Testing binary.

        ``sys.platform`` is read here, at access time, so a test can switch
        platforms with ``monkeypatch``. macOS is the development-only native mode;
        every confined run is Linux.
        """
        root = self.prefix / "chrome"
        if sys.platform == "darwin":
            return (
                root
                / "Google Chrome for Testing.app"
                / "Contents"
                / "MacOS"
                / "Google Chrome for Testing"
            )
        return root / "chrome"

    @property
    def node_modules(self) -> Path:
        """The ``npm ci`` tree installed from the committed lockfile."""
        return self.prefix / "node_modules"

    @property
    def claude(self) -> Path:
        """The pinned Claude Code CLI."""
        return self.node_modules / ".bin" / "claude"

    @property
    def devtools_mcp(self) -> Path:
        """The pinned chrome-devtools-mcp server."""
        return self.node_modules / ".bin" / "chrome-devtools-mcp"

    @property
    def versions_file(self) -> Path:
        """``VERSIONS.json``, written by ``install.sh`` after every install."""
        return self.prefix / "VERSIONS.json"

    def missing(self) -> list[str]:
        """Which of the three binaries the test needs is not an existing file.

        ``Path.is_file`` follows symlinks, and ``node_modules/.bin`` entries are
        symlinks, so a dangling link reads as missing rather than as present.
        """
        binaries = (
            ("chrome", self.chrome),
            ("claude", self.claude),
            ("chrome-devtools-mcp", self.devtools_mcp),
        )
        return [name for name, path in binaries if not path.is_file()]

    def versions(self) -> dict[str, str]:
        """The recorded toolchain versions, or ``{}`` when there is no usable record.

        Absent, unreadable, not JSON, or anything but an object of strings all give
        ``{}``. The versions are reported in the run summary, so a malformed record
        must read as "unknown" rather than raise in the middle of a run that is
        otherwise fine.
        """
        try:
            parsed = json.loads(self.versions_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        if not all(isinstance(value, str) for value in parsed.values()):
            return {}
        return {str(key): value for key, value in parsed.items()}


def toolchain(
    prefix: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Toolchain:
    """The toolchain to run: the argument, then :data:`PREFIX_ENV`, then the default.

    ``environ`` defaults to ``os.environ`` and is a parameter so a test can state
    the environment it means instead of mutating the process's. An empty value at
    either level counts as unset.
    """
    if prefix:
        return Toolchain(Path(prefix))
    source = os.environ if environ is None else environ
    configured = source.get(PREFIX_ENV, "")
    return Toolchain(Path(configured) if configured else DEFAULT_PREFIX)
