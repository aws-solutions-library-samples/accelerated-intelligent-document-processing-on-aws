# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`idp_common`'s lazy submodule loader: one module object per name, and still lazy.

Two properties are in tension and both matter.

**Laziness** is why a Lambda installing `idp_common[core]` does not drag in the `[all]`
dependency set: `import idp_common` must not import any submodule, so nothing pays for
Strands, pypdfium2 or Textract parsing unless it asks for them. `test_import_surface_region_free.py`
holds the related property that importing a module must not need an AWS region.

**Single identity** is why `unittest.mock.patch` can be trusted. The loader used to keep
a package-private `_submodules` dict alongside `sys.modules`, so the same name could
resolve to two different module objects and a patch applied to one was invisible to code
holding the other (#1159). `importlib` already caches in `sys.modules`; a second cache
can only disagree with it.

⚠️ **The tests below do not, and cannot, make a patch immune.** The duplication #1159
reported was created outside this package: `coverage` imports each `--cov=<module>`
target inside its `sys_modules_saved()` context and then deletes every `sys.modules`
entry that import added, while the module objects survive as attributes of their parent
packages. Any later import of such a name re-executes the file and produces a second
object. Nothing in `__init__.py` can prevent that. What removes the whole class of
failure is patching **at the point of use** — see the note on target selection in
`tests/unit/ocr/test_ocr_service.py`, which is not uniform: a module-level
`from idp_common import s3` is patched through the consumer, a function-local
`from idp_common.image import f` through `sys.modules`.
"""

import os
import subprocess  # nosec B404 - fixed argv, no shell, no external input
import sys

import pytest

import idp_common

#: Third-party packages that must not be imported by `import idp_common` alone. Each is
#: in an optional dependency group, so a `[core]` install may not even have it.
_HEAVY_OPTIONAL_DEPENDENCIES = (
    "boto3",
    "botocore",
    "strands",
    "pydantic",
    "PIL",
    "pypdfium2",
    "numpy",
)


def _probe(source: str) -> str:
    """Run ``source`` in a fresh interpreter that can see this checkout.

    Every property here is about the loader, and a loader property cannot be measured
    inside a shared pytest session: by the time this suite runs, half the library is
    imported, and other suites legitimately evict modules from ``sys.modules`` to
    re-exercise an import path. A fresh interpreter is both order-independent and the
    state a Lambda cold start actually starts from.

    ``PYTHONPATH`` is built from the parent's ``sys.path`` rather than inherited, so the
    child resolves the same ``idp_common`` whether this suite is running against an
    editable install (CI) or off ``PYTHONPATH`` (local). Inheriting alone makes the probe
    fail with an ``ImportError`` in one of those two arrangements.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path)
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


@pytest.mark.unit
class TestOneModuleObjectPerName:
    """No cache beside `sys.modules`, so there is nothing for it to disagree with."""

    def test_every_lazy_name_resolves_to_the_sys_modules_object(self):
        """What a patch target and what the consumer holds have to be the same object.

        Checked in a fresh interpreter, and that is not fussiness. Asserted in-process,
        this fails for `agents` in a full-suite run, and the suite that breaks it is
        doing something legitimate: `tests/unit/lambdas/test_agentcore_mcp_handler_tools.py`
        deletes every `idp_common.agents*` entry from `sys.modules` and makes `import
        strands` raise, to prove the AgentCore handler still loads without the agents
        stack. `monkeypatch` then restores the `sys.modules` entries — but the re-import
        in between rebound `idp_common.agents` on the parent package, and nothing
        restores that. The divergence is a property of the session, not of the loader, so
        asserting it here would make this test a tripwire for another suite's ordering.
        """
        mismatched = _probe(
            "import sys, idp_common\n"
            "bad = []\n"
            "for name in sorted(idp_common._LAZY_SUBMODULES):\n"
            "    try:\n"
            "        module = getattr(idp_common, name)\n"
            "    except Exception as exc:\n"
            "        bad.append(f'{name}(import failed: {type(exc).__name__})')\n"
            "        continue\n"
            "    if module is not sys.modules.get(f'idp_common.{name}'):\n"
            "        bad.append(name)\n"
            "print(','.join(bad))"
        )
        assert mismatched == "", (
            f"not the sys.modules object: {mismatched}. A patch applied to one copy "
            "would be invisible to code holding the other"
        )

    def test_there_is_no_second_cache_to_go_stale(self):
        """Named explicitly: reintroducing a private dict reintroduces #1159's symptom.

        The failure it produced was three assertion errors on a call count, pointing
        nowhere near the cause — which is why this is asserted rather than left as a
        comment on the code that must not come back.
        """
        assert not hasattr(idp_common, "_submodules"), (
            "a package-private submodule cache is back; defer to sys.modules instead"
        )

    def test_repeated_access_returns_the_same_object(self):
        assert idp_common.s3 is idp_common.s3

    def test_an_unknown_attribute_still_raises(self):
        with pytest.raises(AttributeError, match="no attribute 'not_a_submodule'"):
            idp_common.not_a_submodule


@pytest.mark.unit
class TestTheLoaderIsStillLazy:
    """The property the #1159 fix had to preserve. See `_probe` for why in a subprocess."""

    def test_importing_the_package_imports_no_submodule(self):
        """`import idp_common` must cost the standard library and nothing else."""
        imported = _probe(
            "import sys, idp_common\n"
            "print(','.join(sorted("
            "  n for n in idp_common._LAZY_SUBMODULES"
            "  if f'idp_common.{n}' in sys.modules)))"
        )
        assert imported == "", f"eagerly imported: {imported}"

    def test_importing_the_package_imports_no_optional_dependency(self):
        """The reason laziness exists: a `[core]` install may not have these at all."""
        names = ",".join(_HEAVY_OPTIONAL_DEPENDENCIES)
        imported = _probe(
            "import sys, idp_common\n"
            f"print(','.join(m for m in {names!r}.split(',') if m in sys.modules))"
        )
        assert imported == "", f"eagerly imported: {imported}"

    def test_touching_one_submodule_does_not_import_the_others(self):
        """Per-name laziness, not just package-level.

        `idp_common.models` is the cheapest of them and reaches none of the heavy
        pipeline modules, which is what makes it a usable probe here.
        """
        imported = _probe(
            "import sys, idp_common\n"
            "idp_common.models\n"
            "print(','.join(sorted("
            "  n for n in idp_common._LAZY_SUBMODULES"
            "  if n != 'models' and f'idp_common.{n}' in sys.modules)))"
        )
        assert "extraction" not in imported, imported
        assert "agents" not in imported, imported
        assert "ocr" not in imported, imported

    @pytest.mark.parametrize("name", sorted(idp_common._LAZY_SUBMODULES))
    def test_every_advertised_name_resolves(self, name):
        """A name in the list that does not import is a silent `AttributeError` waiting
        for whichever caller reaches it first."""
        assert getattr(idp_common, name) is not None

    def test_the_lazy_names_are_all_advertised_in_dunder_all(self):
        """`__all__` and the loader's list are two statements of one fact."""
        assert idp_common._LAZY_SUBMODULES <= set(idp_common.__all__), set(
            idp_common._LAZY_SUBMODULES
        ) - set(idp_common.__all__)
