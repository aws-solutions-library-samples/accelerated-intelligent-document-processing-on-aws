# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""This directory's ``conftest.py`` puts the checkout's ``idp_common`` first.

The conftest adds ``lib/idp_common_pkg`` to ``sys.path`` so a direct ``pytest`` run
reads the library from this checkout. Appended, the entry lost to any installed copy
ahead of it, and the provenance guard at the bottom of the conftest then refused the
whole run (#1758, the ordering #1747 fixed in ``codebuild_deployment.py``).

The installed copy is stood in for by a decoy on ``PYTHONPATH``, which sits ahead of
``site-packages`` and so is the harder case. ``PYTHONPATH`` is replaced rather than
extended, because ``make`` pins it to this checkout and the pin would make the
assertion pass whatever the conftest does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONFTEST = Path(__file__).resolve().parent / "conftest.py"

_PROBE = """
import runpy, sys
runpy.run_path(sys.argv[1], run_name="conftest_probe")
import idp_common
print(idp_common.__file__)
"""


def test_the_checkouts_idp_common_wins_over_an_installed_copy(tmp_path):
    decoy = tmp_path / "installed"
    (decoy / "idp_common").mkdir(parents=True)
    (decoy / "idp_common" / "__init__.py").write_text("")
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "IDP_ALLOW_FOREIGN_FIRST_PARTY"
    }
    env["PYTHONPATH"] = str(decoy)

    # Non-vacuity: without the conftest's entry the decoy is what is imported.
    plain = subprocess.run(
        [sys.executable, "-c", "import idp_common; print(idp_common.__file__)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert Path(plain.stdout.strip()).parent == decoy / "idp_common"

    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(CONFTEST)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    bound = Path(result.stdout.strip().splitlines()[-1]).parent
    assert bound == REPO / "lib" / "idp_common_pkg" / "idp_common"
