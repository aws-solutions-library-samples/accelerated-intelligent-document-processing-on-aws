"""The safety tripwire for the SDLC harness's two CloudFormation YAML loaders.

Both loaders subclass `yaml.SafeLoader` and register a multi-constructor for
`!`-prefixed intrinsics. That is safe — `python/object`, `python/name` and
`python/object/apply` are never registered, so a document cannot instantiate an
object or import a module — but nothing *enforced* it.

Until recently the enforcement was accidental: the `yaml.load(..., Loader=...)`
call shape made Bandit B506 and ACAT fire on these files, so a base-class change
would at least have drawn a scanner's attention. Those call shapes are now gone
(they were false positives that had to be hand-triaged on every scan), which
removes the accidental tripwire — so this file is the deliberate one.

The failure mode it guards is concrete and tempting: a developer hits
`could not determine a constructor for the tag '!Foo'` while parsing a template
and "fixes" it by changing `yaml.SafeLoader` to `yaml.Loader`. In
`validate_service_role_permissions.py` — which takes a template path as a CLI
argument and runs in CI — that single word is a real remote-code-execution
primitive. These tests fail if anyone does it.
"""

import importlib

import pytest
import yaml

# A genuine RCE payload: under yaml.Loader/FullLoader/UnsafeLoader this imports
# os and calls it. `true` is chosen so a regression is visible via the assertions
# rather than by damaging the checkout.
_PAYLOAD = "a: !!python/object/apply:os.system ['true']"

# (module, loader attribute) for every CFN loader in the SDLC harness.
_LOADERS = [
    ("validate_service_role_permissions", "CFNLoader"),
    ("test_iam_trust_policy_partitions", "CfnLoader"),
    # The ConfigSchema sweeps (sibling `order` uniqueness, and the IDPConfig <->
    # UI-schema parity gate that reuses this loader) parse both full templates.
    ("test_config_schema_order", "_CfnSafeLoader"),
]


def _loader(module_name: str, attr: str):
    # conftest.py puts both scripts/sdlc and this tests directory on sys.path.
    return getattr(importlib.import_module(module_name), attr)


@pytest.mark.parametrize(("module_name", "attr"), _LOADERS)
def test_loader_subclasses_safeloader(module_name, attr):
    loader_cls = _loader(module_name, attr)
    assert issubclass(loader_cls, yaml.SafeLoader), (
        f"{module_name}.{attr} is no longer a yaml.SafeLoader subclass — it can "
        "construct arbitrary Python objects from any template it parses"
    )


@pytest.mark.parametrize(("module_name", "attr"), _LOADERS)
def test_loader_does_not_register_python_object_constructors(module_name, attr):
    loader_cls = _loader(module_name, attr)
    registered = loader_cls.yaml_constructors
    for tag in list(registered) + list(loader_cls.yaml_multi_constructors):
        assert "python/" not in str(tag), (
            f"{module_name}.{attr} registers {tag!r}, which can execute code"
        )


@pytest.mark.parametrize(("module_name", "attr"), _LOADERS)
def test_loader_cannot_execute_a_python_tag(module_name, attr):
    """End-to-end: the payload must either be refused or parse to inert data.

    Both outcomes are safe. These loaders register the `!` prefix, so the
    `tag:yaml.org,2002:python/...` tag reaches SafeLoader and is refused; a
    loader registering the catch-all `""` prefix would instead return plain data.
    Accept either, and fail on anything that actually constructed an object.
    """
    loader_cls = _loader(module_name, attr)
    loader = loader_cls(_PAYLOAD)
    try:
        result = loader.get_single_data()
    except yaml.constructor.ConstructorError:
        return  # refused outright
    finally:
        loader.dispose()

    assert isinstance(result["a"], (str, list, dict, type(None))), (
        f"{module_name}.{attr} constructed a {type(result['a']).__name__} from a "
        "python/object/apply payload — it is no longer inert"
    )
