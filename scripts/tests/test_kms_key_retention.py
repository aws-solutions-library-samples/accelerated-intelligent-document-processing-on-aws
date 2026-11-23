# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A KMS key that encrypts a retained resource is retained too (#2011).

Most of the main stack's S3 buckets survive a stack delete (`DeletionPolicy:
Retain` or `RetainExceptOnCreate`) and are encrypted with
`CustomerManagedEncryptionKey`. A key that CloudFormation deleted would leave
every object in them unreadable once its deletion window ended, so the key
carries the same policies.

The rule is checked over every template content discovery finds, not only
`template.yaml`: any resource a template retains that references a KMS key the
same template declares makes that key a retained one. A key reached across a
stack boundary (an ARN passed to a nested stack as a parameter) is outside what
this reads; today no nested template retains a resource encrypted with the
parent's key, and the derived set below is asserted non-empty so the check
cannot pass by finding nothing.

The headless and GovCloud transforms are run over the real template (in the
long form `sam build` gives them) and their output re-read from disk, because a transform that rebuilt a resource from its
`Type` and `Properties` would drop both policies without failing anything else.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_KEY = "CustomerManagedEncryptionKey"
RETAINING = {"Retain", "RetainExceptOnCreate", "Snapshot"}


def _load(path: Path) -> dict:
    from idp_sdk._core.cfn_yaml import load_cfn_template

    return load_cfn_template(str(path))


def _names(node: Any) -> set[str]:
    """Every logical id ``node`` references through Ref, GetAtt or Sub, in
    long-form or short-form (``!Ref``) spelling."""
    out: set[str] = set()
    if isinstance(node, list):
        for x in node:
            out |= _names(x)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in ("Ref", "!Ref") and isinstance(v, str):
                out.add(v.split(".", 1)[0])
            elif k in ("Fn::GetAtt", "!GetAtt"):
                out.add((v[0] if isinstance(v, list) else str(v)).split(".", 1)[0])
            elif k in ("Fn::Sub", "!Sub"):
                text = v[0] if isinstance(v, list) else v
                if isinstance(text, str):
                    out |= {
                        m.split(".", 1)[0]
                        for m in re.findall(r"\$\{([^}!][^}]*)\}", text)
                    }
                if isinstance(v, list) and len(v) > 1:
                    out |= _names(v[1])
            else:
                out |= _names(v)
    else:
        tag = getattr(node, "tag", None)
        value = getattr(node, "value", None)
        if tag is not None:
            out |= _names({tag: value})
    return out


def _keys_protecting_retained_data(template: dict) -> dict[str, list[str]]:
    """KMS key logical id -> the retained resources that reference it."""
    resources = template.get("Resources") or {}
    keys = {lid for lid, r in resources.items() if r.get("Type") == "AWS::KMS::Key"}
    out: dict[str, list[str]] = {}
    for lid, r in resources.items():
        if lid in keys or r.get("DeletionPolicy") not in RETAINING:
            continue
        for key in sorted(_names(r.get("Properties") or {}) & keys):
            out.setdefault(key, []).append(lid)
    return out


def _templates() -> list[Path]:
    listed = subprocess.run(  # noqa: S603 - fixed argv
        ["bash", "scripts/discover_templates.sh", "cfn"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.split()
    return [REPO_ROOT / p for p in listed]


def test_the_main_key_carries_both_retain_policies():
    key = _load(REPO_ROOT / "template.yaml")["Resources"][MAIN_KEY]
    assert key.get("DeletionPolicy") == "Retain"
    assert key.get("UpdateReplacePolicy") == "Retain"


def test_the_main_key_encrypts_retained_buckets():
    """The premise of the policy, derived rather than restated: if no retained
    bucket used the key any more, the retention would need revisiting."""
    users = _keys_protecting_retained_data(_load(REPO_ROOT / "template.yaml"))
    buckets = users.get(MAIN_KEY, [])
    assert "InputBucket" in buckets and "OutputBucket" in buckets, buckets


def test_the_alias_is_not_retained():
    """A retained alias would make a new stack of the same name fail to create
    its own; the key is found through a retained bucket's encryption instead."""
    alias = _load(REPO_ROOT / "template.yaml")["Resources"][f"{MAIN_KEY}Alias"]
    assert alias.get("DeletionPolicy") in (None, "Delete")


def test_every_key_encrypting_retained_data_is_retained():
    templates = _templates()
    assert len(templates) > 1, "template discovery returned too little to check"
    seen: list[str] = []
    unretained: list[str] = []
    for path in templates:
        template = _load(path)
        resources = template.get("Resources") or {}
        for key, users in _keys_protecting_retained_data(template).items():
            rel = path.relative_to(REPO_ROOT)
            seen.append(f"{rel}:{key}")
            res = resources[key]
            if not (
                res.get("DeletionPolicy") in RETAINING
                and res.get("UpdateReplacePolicy") in RETAINING
            ):
                unretained.append(f"{rel}:{key} (encrypts {', '.join(users)})")
    assert f"template.yaml:{MAIN_KEY}" in seen
    assert unretained == []


def test_a_key_left_deletable_is_reported():
    """The check above fails on the defect it exists for."""
    template = {
        "Resources": {
            "Key": {"Type": "AWS::KMS::Key"},
            "Bucket": {
                "Type": "AWS::S3::Bucket",
                "DeletionPolicy": "RetainExceptOnCreate",
                "Properties": {
                    "BucketEncryption": {
                        "ServerSideEncryptionConfiguration": [
                            {
                                "ServerSideEncryptionByDefault": {
                                    "KMSMasterKeyID": {"Ref": "Key"}
                                }
                            }
                        ]
                    }
                },
            },
            "Gone": {
                "Type": "AWS::S3::Bucket",
                "Properties": {"X": {"Fn::GetAtt": ["Key", "Arn"]}},
            },
        }
    }
    assert _keys_protecting_retained_data(template) == {"Key": ["Bucket"]}


def _long_form_template() -> dict:
    """template.yaml as the publisher hands it to a transform: long-form
    intrinsics (`sam build` output), as plain dicts, lists and strings."""
    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")

    def plain(node: Any) -> Any:
        if isinstance(node, dict):
            return {str(k): plain(v) for k, v in node.items()}
        if isinstance(node, list):
            return [plain(x) for x in node]
        if isinstance(node, str):
            return str(node)
        return node

    return plain(cfnlint_decode.load(str(REPO_ROOT / "template.yaml")))


@pytest.mark.parametrize("variant", ["headless", "govcloud"])
def test_the_transforms_keep_both_policies(variant: str, tmp_path: Path):
    import yaml

    from idp_sdk._core.template_transform import (
        GovCloudTemplateTransformer,
        HeadlessTemplateTransformer,
    )

    transformer = (
        HeadlessTemplateTransformer()
        if variant == "headless"
        else GovCloudTemplateTransformer()
    )
    template = _long_form_template()
    assert template["Resources"][MAIN_KEY].get("DeletionPolicy") == "Retain"
    out = tmp_path / f"{variant}.yaml"
    transformer.save_template(transformer.apply_transforms(template), str(out))
    key = yaml.safe_load(out.read_text())["Resources"][MAIN_KEY]
    assert key.get("DeletionPolicy") == "Retain"
    assert key.get("UpdateReplacePolicy") == "Retain"
