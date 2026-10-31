# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""All Inbox DynamoDB access goes through idp_common.dynamodb (S.3).

The only raw ``boto3.client(...)`` allowed in the package is ``cognito-idp`` in
``recipients.py``, because idp_common has no Cognito wrapper.
"""

import ast
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[3] / "idp_common" / "notifications"
_FORBIDDEN_SERVICES = {"dynamodb", "dynamodb-streams"}


def _boto3_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"client", "resource"}
            and isinstance(func.value, ast.Name)
            and func.value.id == "boto3"
        ):
            arg = node.args[0] if node.args else None
            service = arg.value if isinstance(arg, ast.Constant) else None
            yield func.attr, service, node.lineno


@pytest.mark.unit
class TestNoRawBoto3DynamoDB:
    def test_package_files_exist(self):
        assert len(list(_PKG.glob("*.py"))) >= 8

    def test_no_raw_dynamodb_and_only_cognito_client(self):
        offenders = []
        allowed_seen = []
        for path in sorted(_PKG.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for kind, service, line in _boto3_calls(tree):
                where = f"{path.name}:{line} boto3.{kind}({service!r})"
                if service in _FORBIDDEN_SERVICES:
                    offenders.append(where)
                elif (kind, service, path.name) == (
                    "client",
                    "cognito-idp",
                    "recipients.py",
                ):
                    allowed_seen.append(where)
                else:
                    offenders.append(where)
        assert offenders == [], offenders
        assert len(allowed_seen) == 1, "the cognito-idp client should exist once"
