# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the shared CloudFormation YAML loader.

The inertness assertions here are the point of the file, not a formality: a
CFN-tolerant YAML loader is exactly the shape a scanner flags as unsafe
deserialization (CWE-94), and this suite is the standing evidence that the
flagged capability does not exist. If someone later switches the base class to
`yaml.Loader` or `yaml.FullLoader` to "fix" a tag that won't parse, these fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from idp_sdk._core.cfn_yaml import (
    load_cfn_template,
    load_cfn_yaml,
    make_cfn_loader,
    preserve_intrinsics,
)

pytestmark = pytest.mark.unit


# --- Safety: no Python object construction, ever ------------------------------

# Each payload is a real RCE attempt against PyYAML's unsafe constructors. Under
# `yaml.Loader`/`FullLoader` these import a module and call into it; under our
# SafeLoader subclass they must not.
_PYTHON_TAG_PAYLOADS = [
    "a: !!python/object/apply:os.system ['true']",
    "a: !!python/name:os.system {}",
    "a: !!python/object/new:subprocess.Popen [['true']]",
    "a: !!python/module:os {}",
]


@pytest.mark.parametrize("payload", _PYTHON_TAG_PAYLOADS)
def test_python_tags_are_refused_by_the_default_loader(payload):
    """With the default `!` prefix, python tags reach SafeLoader and are refused.

    The tag `tag:yaml.org,2002:python/...` does not start with `!`, so our
    multi-constructor never sees it and SafeLoader has no constructor for it.
    """
    with pytest.raises(yaml.constructor.ConstructorError):
        load_cfn_yaml(payload)


@pytest.mark.parametrize("payload", _PYTHON_TAG_PAYLOADS)
def test_python_tags_collapse_to_inert_data_under_the_catch_all_prefix(payload):
    """With `prefix=""` the same payloads parse, but only into plain data.

    No import, no instantiation, no call — the constructor's argument list
    survives as a list of strings and nothing is executed.
    """
    result = load_cfn_yaml(payload, make_cfn_loader(prefix=""))
    assert isinstance(result, dict)
    value = result["a"]
    assert isinstance(value, (str, list, dict)), (
        f"expected inert data, got {type(value).__name__}: {value!r}"
    )


def test_a_payload_that_would_write_a_file_does_not(tmp_path: Path):
    """The strongest form of the assertion: a side effect that would be visible.

    `os.system` writing a marker file is observable, so this fails loudly if the
    loader ever gains the ability to execute its input.
    """
    marker = tmp_path / "pwned"
    payload = f"a: !!python/object/apply:os.system ['touch {marker}']"

    with pytest.raises(yaml.constructor.ConstructorError):
        load_cfn_yaml(payload)
    assert not marker.exists()

    # ...and under the catch-all prefix, where it parses rather than raising.
    load_cfn_yaml(payload, make_cfn_loader(prefix=""))
    assert not marker.exists(), "loader executed its input — this is an RCE"


def test_loader_subclasses_safeloader():
    """Asserted directly so a base-class change fails here with a clear reason,
    not as a confusing downstream parse error.

    Note there is deliberately no `assert not issubclass(..., yaml.UnsafeLoader)`
    here: in PyYAML `Loader`, `FullLoader` and `UnsafeLoader` are siblings rather
    than ancestors of each other, so such an assertion holds for every loader —
    including an unsafe one — and would read as coverage it does not provide.
    """
    assert issubclass(make_cfn_loader(), yaml.SafeLoader)
    # The constructors that make yaml.load dangerous must be absent.
    for unsafe in (
        "tag:yaml.org,2002:python/object/apply:os.system",
        "tag:yaml.org,2002:python/name:os.system",
    ):
        assert unsafe not in make_cfn_loader().yaml_constructors


def test_an_unsafe_loader_class_is_refused():
    """The whole point of this module is that its entry points cannot execute a
    document, so they must not accept a loader that can.

    Without this, `load_cfn_yaml(text, yaml.UnsafeLoader)` executes its input
    while sitting behind a module documented as safe — the exact confusion this
    module exists to end.
    """
    for unsafe in (yaml.UnsafeLoader, yaml.Loader, yaml.FullLoader):
        with pytest.raises(TypeError, match="SafeLoader"):
            load_cfn_yaml("a: 1", unsafe)


def test_the_file_entry_point_refuses_an_unsafe_loader_too(tmp_path: Path):
    path = tmp_path / "t.yaml"
    path.write_text("a: 1", encoding="utf-8")
    with pytest.raises(TypeError, match="SafeLoader"):
        load_cfn_template(path, yaml.UnsafeLoader)


# --- Behaviour: intrinsics stay inspectable -----------------------------------


def test_intrinsics_are_preserved_as_tagged_dicts():
    doc = load_cfn_yaml(
        "Resources:\n"
        "  Fn:\n"
        "    Properties:\n"
        "      Role: !GetAtt MyRole.Arn\n"
        "      Name: !Sub '${AWS::StackName}-fn'\n"
        "      Bucket: !Ref MyBucket\n"
    )
    props = doc["Resources"]["Fn"]["Properties"]
    assert props["Role"] == {"!GetAtt": "MyRole.Arn"}
    assert props["Name"] == {"!Sub": "${AWS::StackName}-fn"}
    assert props["Bucket"] == {"!Ref": "MyBucket"}


def test_nested_intrinsics_are_constructed_deeply():
    """`deep=True` matters: without it a nested intrinsic inside an `!If` comes
    back as an unconstructed node object that no assertion can read."""
    doc = load_cfn_yaml("Value: !If [IsProd, !Ref ProdArn, !Ref DevArn]")
    assert doc["Value"] == {"!If": ["IsProd", {"!Ref": "ProdArn"}, {"!Ref": "DevArn"}]}


def test_plain_yaml_is_untouched():
    doc = load_cfn_yaml("a: 1\nb: [x, y]\nc: {d: true}\ne: null\n")
    assert doc == {"a": 1, "b": ["x", "y"], "c": {"d": True}, "e": None}


def test_each_loader_is_a_fresh_class():
    """`add_multi_constructor` mutates the class it is called on, so a shared
    class would leak one caller's tag policy into another's."""
    calls = []

    def _record(_loader, tag_suffix, _node):
        calls.append(tag_suffix)
        return None

    custom = make_cfn_loader(_record)
    assert load_cfn_yaml("a: !Ref Thing", custom) == {"a": None}
    assert calls == ["Ref"]

    # The default loader must be unaffected by the registration above.
    assert load_cfn_yaml("a: !Ref Thing") == {"a": {"!Ref": "Thing"}}
    assert calls == ["Ref"]


def test_custom_constructor_policy_is_honoured():
    """The `None`-collapsing policy that `validate_service_role_permissions.py`
    depends on."""

    def _drop(_loader, _tag_suffix, _node):
        return None

    doc = load_cfn_yaml("a: !Ref X\nb: plain\n", make_cfn_loader(_drop))
    assert doc == {"a": None, "b": "plain"}


def test_preserve_intrinsics_is_the_documented_default():
    assert load_cfn_yaml("a: !Ref X") == load_cfn_yaml(
        "a: !Ref X", make_cfn_loader(preserve_intrinsics)
    )


# --- File loading -------------------------------------------------------------


def test_load_cfn_template_reads_a_file(tmp_path: Path):
    path = tmp_path / "template.yaml"
    path.write_text("Resources:\n  Q:\n    Type: AWS::SQS::Queue\n", encoding="utf-8")
    assert load_cfn_template(path)["Resources"]["Q"]["Type"] == "AWS::SQS::Queue"
    # str paths too — callers pass both.
    assert load_cfn_template(str(path))["Resources"]


def test_an_empty_template_is_an_empty_dict(tmp_path: Path):
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    assert load_cfn_template(path) == {}


def test_an_unparseable_template_raises(tmp_path: Path):
    """Never degrade to `{}` on a parse error: a caller asserting "no wildcard
    IAM actions" would then pass vacuously on a template it could not read."""
    path = tmp_path / "broken.yaml"
    path.write_text("Resources:\n  - [unclosed\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        load_cfn_template(path)


def test_a_missing_template_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_cfn_template(tmp_path / "nope.yaml")


def test_the_repos_own_main_template_parses():
    """An end-to-end check against the real thing: the main stack template
    exercises far more intrinsic shapes than any fixture here."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            template = load_cfn_template(parent / "template.yaml")
            assert template["Resources"], "main template parsed with no resources"
            return
    pytest.skip("repo root not found; running outside a source checkout")
