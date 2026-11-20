# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The one spelling ``template.yaml`` uses for "the deployment's KMS key" (#2016).

The main template encrypts its data with ``CustomerManagedEncryptionKey`` unless
``CustomerManagedKmsKeyArn`` supplies a key, in which case the stack creates no
key and every reference is the supplied ARN. Each reference is therefore one
``Fn::If`` whose branches are complementary by construction:

- ``UseSuppliedKmsKey`` (the parameter is non-empty) selects ``!GetAtt
  CustomerManagedKmsKeyCheck.KeyArn``, the supplied ARN as the check echoes it,
  so nothing uses a supplied key before the check has accepted it;
- otherwise the key the stack creates, which carries ``Condition:
  CreateStackKmsKey`` (the parameter is empty).

So the value is never empty and never names an absent resource, which is what
gates that used to accept only ``!GetAtt CustomerManagedEncryptionKey.Arn`` need
to know. :func:`complementary` checks the premise against the template itself, so
a gate that accepts the form cannot outlive the conditions that make it safe.

Both spellings the loaders produce are accepted: the long form
(``Fn::If``/``Fn::GetAtt``) and ``load_cfn_template``'s short-tag keys
(``!If``/``!GetAtt``).
"""

from __future__ import annotations

from typing import Any

KEY = "CustomerManagedEncryptionKey"
PARAMETER = "CustomerManagedKmsKeyArn"
USE_SUPPLIED = "UseSuppliedKmsKey"
CREATE_OWN = "CreateStackKmsKey"
CHECK = "CustomerManagedKmsKeyCheck"


def _one(node: Any, *names: str) -> Any:
    if isinstance(node, dict) and len(node) == 1:
        ((k, v),) = node.items()
        if k in names:
            return v
    return None


def _ref(node: Any) -> Any:
    return _one(node, "Ref", "!Ref")


def is_key_arn(node: Any) -> bool:
    """``!GetAtt CustomerManagedEncryptionKey.Arn`` in any spelling."""
    v = _one(node, "Fn::GetAtt", "!GetAtt")
    return v in (f"{KEY}.Arn", [KEY, "Arn"])


def is_key_id(node: Any) -> bool:
    """``!Ref CustomerManagedEncryptionKey``."""
    return _ref(node) == KEY


def is_checked_supplied_key(node: Any) -> bool:
    """``!GetAtt CustomerManagedKmsKeyCheck.KeyArn`` in any spelling."""
    v = _one(node, "Fn::GetAtt", "!GetAtt")
    return v in (f"{CHECK}.KeyArn", [CHECK, "KeyArn"])


def branches(node: Any) -> tuple[Any, Any] | None:
    """``(supplied, own)`` when ``node`` is ``Fn::If [UseSuppliedKmsKey, a, b]``."""
    v = _one(node, "Fn::If", "!If")
    if isinstance(v, list) and len(v) == 3 and v[0] == USE_SUPPLIED:
        return v[1], v[2]
    return None


def is_deployment_key_arn(node: Any) -> bool:
    """The deployment key's ARN: the supplied one, else the stack key's."""
    b = branches(node)
    return b is not None and is_checked_supplied_key(b[0]) and is_key_arn(b[1])


def is_deployment_key_id(node: Any) -> bool:
    """The deployment key as a KMS key id or ARN (``!Ref`` on the stack key)."""
    b = branches(node)
    return b is not None and is_checked_supplied_key(b[0]) and is_key_id(b[1])


def complementary(template: dict) -> list[str]:
    """What is wrong with the premise that makes the form safe; empty if none."""
    problems = []
    conditions = template.get("Conditions") or {}
    equals = {"Fn::Equals": [{"Ref": PARAMETER}, ""]}
    equals_short = {"!Equals": [{"!Ref": PARAMETER}, ""]}

    def is_equals(c: Any) -> bool:
        return c in (equals, equals_short)

    def is_not_equals(c: Any) -> bool:
        inner = _one(c, "Fn::Not", "!Not")
        return isinstance(inner, list) and len(inner) == 1 and is_equals(inner[0])

    if not is_not_equals(conditions.get(USE_SUPPLIED)):
        problems.append(
            f"{USE_SUPPLIED} is not `!Not [!Equals [!Ref {PARAMETER}, '']]`"
        )
    if not is_equals(conditions.get(CREATE_OWN)):
        problems.append(f"{CREATE_OWN} is not `!Equals [!Ref {PARAMETER}, '']`")
    key = (template.get("Resources") or {}).get(KEY) or {}
    if key.get("Condition") != CREATE_OWN:
        problems.append(f"{KEY} does not carry `Condition: {CREATE_OWN}`")
    param = (template.get("Parameters") or {}).get(PARAMETER) or {}
    if param.get("Default") != "":
        problems.append(f"{PARAMETER} does not default to ''")
    check = (template.get("Resources") or {}).get(CHECK) or {}
    key_arn = (check.get("Properties") or {}).get("KeyArn")
    if "Condition" in check or _ref(key_arn) != PARAMETER:
        problems.append(
            f"{CHECK} is not an unconditional resource with `KeyArn: !Ref {PARAMETER}`"
        )
    return problems
