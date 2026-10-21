# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review-queue routing runs with only the extras the Pipeline ProcessResults installs.

That Lambda installs ``idp_common_pkg[core,docs_service,review_queues]``. Routing
once reached ``idp_common.assessment`` for the class-schema lookup, whose package
``__init__`` loads the assessment service and Pillow, so every Pipeline-mode
document failed with ``No module named 'PIL'`` as soon as the Default queue was
enabled. The unit suite installs every extra, so nothing else here could see it.

The child process refuses to import any distribution that only an extra the Lambda
does not install brings in, then drives the routing path that reads a section's
confidences and looks up its class schema. Both lists are read from the files the
build reads, so a new extra or a changed requirements line is covered unchanged.
"""

import json
import os
import re
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parents[3]
_REPO_ROOT = _PKG_ROOT.parents[1]
_REQUIREMENTS = (
    _REPO_ROOT
    / "patterns"
    / "unified"
    / "src"
    / "processresults_function"
    / "requirements.txt"
)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _dist(requirement: str) -> str:
    match = _NAME.match(requirement.strip())
    assert match, requirement
    return match.group(0).lower().replace("_", "-")


def _installed_extras() -> list[str]:
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    match = re.search(r"idp_common_pkg\[([^\]]+)\]", text)
    assert match, f"{_REQUIREMENTS} no longer installs idp_common_pkg with extras"
    return [extra.strip() for extra in match.group(1).split(",")]


def _blocked_modules() -> list[str]:
    """Top-level modules of distributions the Lambda does not install."""
    project = tomllib.loads((_PKG_ROOT / "pyproject.toml").read_text())["project"]
    extras = project["optional-dependencies"]
    installed = {_dist(r) for r in project["dependencies"]}
    for extra in _installed_extras():
        installed |= {_dist(r) for r in extras[extra]}
    elsewhere = {
        _dist(r)
        for name, reqs in extras.items()
        if name not in _installed_extras()
        for r in reqs
        if not r.startswith("idp_common")
    } - installed
    modules = set()
    for module, dists in packages_distributions().items():
        if {d.lower().replace("_", "-") for d in dists} & elsewhere:
            modules.add(module)
    return sorted(m for m in modules if m.isidentifier())


_CHILD = r"""
import importlib.abc
import json
import sys

BLOCKED = set(json.loads(sys.argv[1]))


class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"No module named '{name}'")
        return None


sys.meta_path.insert(0, Refuse())

from types import SimpleNamespace

from idp_common.review_queues.reasons import build_reasons

result = {"explainability_info": [{"Employer": {"confidence": 0.4, "confidence_threshold": 0.9}}]}
s3 = SimpleNamespace(get_json_content=lambda uri: result)
section = SimpleNamespace(section_id="1", classification="W2", extraction_result_uri="s3://b/k")
config = SimpleNamespace(classes=[{"x-aws-idp-document-type": "W2", "properties": {}}])
reasons = build_reasons(
    SimpleNamespace(sections=[section]), config, threshold=0.8, mode="pipeline", s3=s3
)
import idp_common.review_queues.routing  # noqa: F401
print(json.dumps([r.field for r in reasons]))
"""


@pytest.mark.unit
class TestRoutingImportWeight:
    def test_the_blocked_set_covers_pillow(self):
        # Non-vacuous: the regression this guards was Pillow, from the image extra.
        assert "PIL" in _blocked_modules()

    def test_routing_reads_confidences_without_the_extras_the_lambda_lacks(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        run = subprocess.run(  # nosec B603 - fixed argv, no shell
            [sys.executable, "-c", _CHILD, json.dumps(_blocked_modules())],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        assert run.returncode == 0, run.stderr
        assert json.loads(run.stdout.strip().splitlines()[-1]) == ["Employer"]
