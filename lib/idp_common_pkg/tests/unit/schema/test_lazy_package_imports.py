# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``idp_common.schema.multi_instance`` must be importable without the extraction extra.

``schema/pydantic_generator.py`` imports ``datamodel_code_generator``, which is an
extraction-only dependency and is deliberately absent from most Lambda layers.
While ``schema/__init__.py`` re-exported it eagerly, importing the pure
``multi_instance`` module dragged the whole code generator in with it — so every
Lambda without the extraction extra died at import.

Found the hard way: `config/models.py` validates class schemas and needs the
multi-instance helper, `config` is imported by everything, and the
``UpdateDefaultConfig`` custom resource failed a live stack update with
``No module named 'datamodel_code_generator'``. A unit test is much cheaper than a
20-minute CloudFormation rollback.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PKG_ROOT = Path(__file__).resolve().parents[3]


def _run(code: str) -> subprocess.CompletedProcess:
    """Run ``code`` in a FRESH interpreter.

    In-process assertions cannot work: the test session has already imported the
    generator via other tests, so ``sys.modules`` is polluted before this file is
    even collected.
    """
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(PKG_ROOT),
    )


def test_importing_the_transform_does_not_import_the_code_generator():
    result = _run(
        "import sys\n"
        "from idp_common.schema.multi_instance import wrap_class_schema\n"
        "assert 'datamodel_code_generator' not in sys.modules, "
        "'importing multi_instance pulled in datamodel_code_generator'\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_importing_the_config_models_does_not_import_the_code_generator():
    """`config.models` is imported by every Lambda; the multi-instance validator
    it now runs must not change that."""
    result = _run(
        "import sys\n"
        "from idp_common.config.models import IDPConfig\n"
        "IDPConfig(classes=[{'$id': 'X', 'type': 'object', "
        "'x-aws-idp-multi-instance': True, 'properties': {'a': {'type': 'string'}}}])\n"
        "assert 'datamodel_code_generator' not in sys.modules, "
        "'config validation pulled in datamodel_code_generator'\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_the_lazy_re_exports_still_work():
    """The package's public API must be unchanged for existing callers."""
    result = _run(
        "import idp_common.schema as s\n"
        "for name in s.__all__:\n"
        "    assert getattr(s, name) is not None, name\n"
        "from idp_common.schema import create_pydantic_model_from_json_schema\n"
        "assert callable(create_pydantic_model_from_json_schema)\n"
        "assert 'create_pydantic_model_from_json_schema' in dir(s)\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_an_unknown_attribute_still_raises_attribute_error():
    result = _run(
        "import idp_common.schema as s\n"
        "try:\n"
        "    s.definitely_not_a_symbol\n"
        "except AttributeError:\n"
        "    print('ok')\n"
        "else:\n"
        "    raise SystemExit('expected AttributeError')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
