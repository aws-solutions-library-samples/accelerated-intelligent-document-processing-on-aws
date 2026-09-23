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


# ---------------------------------------------------------------------------
# The same two behaviours, asserted IN PROCESS (#1175)
#
# The subprocess tests above are the right shape for what they prove -- that a
# fresh interpreter importing `multi_instance` does not pull in the code generator
# -- and a fresh interpreter is the only way to prove it, because this session has
# already imported the generator by the time this file is collected.
#
# What they are NOT is a coverage instrument. Whether a subprocess's lines are
# counted depends on the environment: measured on this repository, `__getattr__`'s
# AttributeError branch and `__dir__` show as covered locally and as the only two
# uncovered lines of `schema/__init__.py` in GitHub Actions, taking the file from
# 100% to 84.62% there. That put the per-file coverage ratchet permanently red on
# every branch for a module nobody had touched, pointing at a file whose real
# in-process coverage had not changed.
#
# Neither behaviour needs a subprocess: neither imports anything. Asserting them
# here makes the recorded 100% mean the same thing in every environment, which is
# the property a ratchet needs from its baseline.
# ---------------------------------------------------------------------------


def test_dir_lists_every_lazy_export():
    """``__dir__`` must advertise the lazy names, or tab-completion and ``inspect``
    see an empty package until something has touched each attribute.

    Deliberately does NOT also assert that listing avoids importing the generator.
    Whether ``datamodel_code_generator`` is in ``sys.modules`` by the time this file
    runs depends on what ran before it, so the interesting half of that claim is
    only decidable in a fresh interpreter — which is what
    ``test_importing_the_transform_does_not_import_the_code_generator`` above uses a
    subprocess for. An in-process version would pass without testing anything
    whenever an earlier test had already imported it.
    """
    import idp_common.schema as schema

    listed = dir(schema)
    for name in schema.__all__:
        assert name in listed, f"{name} is in __all__ but not in dir()"


def test_an_unknown_attribute_raises_attribute_error_in_process():
    """``__getattr__``'s reject branch. A module ``__getattr__`` that returns None
    or raises the wrong type for an unknown name breaks ``hasattr`` and every
    ``getattr(mod, name, default)`` in the codebase."""
    import idp_common.schema as schema

    with pytest.raises(AttributeError, match="definitely_not_a_symbol"):
        schema.definitely_not_a_symbol  # noqa: B018 - attribute access IS the test

    # The message names the module as well, which is what makes the failure legible
    # when it surfaces through a getattr several frames away.
    try:
        schema.another_missing_symbol
    except AttributeError as exc:
        assert "idp_common.schema" in str(exc)
