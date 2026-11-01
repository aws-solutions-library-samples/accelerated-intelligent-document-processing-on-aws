# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``codebuild_deployment.py`` reads ``idp_common`` from the checkout it deploys.

CodeBuild runs ``make setup`` and then ``python3 scripts/sdlc/codebuild_deployment.py``
(the buildspec in ``scripts/sdlc/cfn/codepipeline-s3.yml``), so an installed
``idp_common`` is importable before the script adds the checkout to ``sys.path``. The
checkout has to come first or the installed copy is the one read (#1747).

The installed copy is stood in for by a decoy package on ``PYTHONPATH``, which sits
ahead of ``site-packages`` and so is the harder case: a checkout entry that beats it
beats an editable-install pointer too. ``PYTHONPATH`` is replaced rather than
extended, because ``make`` pins it to this checkout and the pin would make the
assertion pass whatever the script does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "sdlc" / "codebuild_deployment.py"

# Runs the module's import preamble as the buildspec does, with the script's
# directory as sys.path[0] in place of the "" that -c puts there, so the path the
# module edits has exactly the shape a `python3 <script>` run gives it; and
# reports which idp_common it bound.
_PROBE = """
import json, runpy, sys
sys.path[0] = sys.argv[1]
sys.argv = [sys.argv[2]]
module = runpy.run_path(sys.argv[0], run_name="not_main")
import idp_common, idp_common.stepfunctions_history as h
print(json.dumps({"package": idp_common.__file__, "rule": h.__file__,
                  "bound": module["failing_state"].__module__}))
"""


def _decoy(root: Path) -> Path:
    package = root / "idp_common"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "stepfunctions_history.py").write_text(
        "def failing_state(*a, **k):\n    return None\n\n"
        "def failing_state_is_resolvable(*a, **k):\n    return False\n"
    )
    return root


def test_the_checkouts_idp_common_wins_over_an_installed_copy(tmp_path):
    decoy = _decoy(tmp_path / "installed")
    env = dict(os.environ, PYTHONPATH=str(decoy))
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(SCRIPT.parent), str(SCRIPT)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    checkout = REPO / "lib" / "idp_common_pkg" / "idp_common"

    # Non-vacuity: the decoy is importable ahead of site-packages, so without the
    # script's own path entry it is what `import idp_common` would find.
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

    assert Path(seen["package"]).parent == checkout
    assert Path(seen["rule"]).parent == checkout
    assert seen["bound"] == "idp_common.stepfunctions_history"
