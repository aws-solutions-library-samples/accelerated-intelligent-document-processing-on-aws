# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for scripts/check_data_plane_tags.py.

The linter enforces ``idp:plane=data`` on the small allowlist of
data-plane Lambdas. See docs/reporting-sql-layer.md §10.3.

These tests pin the linter's behavior:
- Whitelisted Lambda missing the tag → exit 1 with the logical ID named
- Whitelisted Lambda missing entirely → exit 1 with "out of date" msg
- Whitelisted Lambda tagged correctly → exit 0
- Wrong tag value (`idp:plane=control`) on a allowlisted Lambda → exit 1
- Both list-of-dicts and dict-form Tags supported (native CFN vs SAM)
- Repo templates (main + unified) actually pass the check today
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LINTER = REPO_ROOT / "scripts" / "check_data_plane_tags.py"


def _load_linter():
    """Load the linter module by path (its dirname isn't on sys.path)."""
    spec = importlib.util.spec_from_file_location("linter", LINTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def linter():
    return _load_linter()


@pytest.mark.unit
class TestTagValue:
    """The _tag_value helper is what turns a resource dict into a
    'is the tag present?' answer. Its return value drives the linter's
    entire pass/fail decision, so the two ways CFN accepts Tags — list
    of dicts vs dict — both need to work.
    """

    def test_list_form_returns_value(self, linter):
        resource = {
            "Properties": {
                "Tags": [
                    {"Key": "idp:plane", "Value": "data"},
                    {"Key": "other", "Value": "x"},
                ]
            }
        }
        assert linter._tag_value(resource, "idp:plane") == "data"

    def test_dict_form_returns_value(self, linter):
        resource = {"Properties": {"Tags": {"idp:plane": "data"}}}
        assert linter._tag_value(resource, "idp:plane") == "data"

    def test_missing_tag_returns_none(self, linter):
        resource = {"Properties": {"Tags": [{"Key": "other", "Value": "x"}]}}
        assert linter._tag_value(resource, "idp:plane") is None

    def test_no_tags_block_returns_none(self, linter):
        resource = {"Properties": {}}
        assert linter._tag_value(resource, "idp:plane") is None

    def test_no_properties_block_returns_none(self, linter):
        # Malformed but shouldn't crash.
        resource = {}
        assert linter._tag_value(resource, "idp:plane") is None


@pytest.mark.unit
class TestCheckWhitelistedLambda:
    """Given (template, logical_id), the check returns [] when tagged and
    a list of error messages when not. This is the core enforcement unit."""

    def _make_template(self, tmp_path: Path, body: str) -> Path:
        """Write a template file and return its path."""
        p = tmp_path / "template.yaml"
        p.write_text(dedent(body))
        return p

    def test_tagged_lambda_passes(self, linter, tmp_path):
        template = self._make_template(
            tmp_path,
            """
            Resources:
              MyDataFn:
                Type: AWS::Serverless::Function
                Properties:
                  Handler: index.handler
                  Runtime: python3.12
                  Tags:
                    idp:plane: data
        """,
        )
        assert linter._check_allowlisted_lambda(template, "MyDataFn") == []

    def test_untagged_lambda_fails_with_logical_id(self, linter, tmp_path):
        """The failure message must name the logical ID — reviewers need
        to know WHERE to add the tag."""
        template = self._make_template(
            tmp_path,
            """
            Resources:
              MyDataFn:
                Type: AWS::Serverless::Function
                Properties:
                  Handler: index.handler
        """,
        )
        errors = linter._check_allowlisted_lambda(template, "MyDataFn")
        assert len(errors) == 1
        assert "MyDataFn" in errors[0]
        assert "missing" in errors[0].lower()

    def test_wrong_tag_value_fails(self, linter, tmp_path):
        """A Lambda explicitly tagged `idp:plane=control` while on the
        data-plane allowlist is likely a classification error — flag it,
        don't silently accept."""
        template = self._make_template(
            tmp_path,
            """
            Resources:
              MyDataFn:
                Type: AWS::Serverless::Function
                Properties:
                  Handler: index.handler
                  Tags:
                    idp:plane: control
        """,
        )
        errors = linter._check_allowlisted_lambda(template, "MyDataFn")
        assert len(errors) == 1
        assert "MyDataFn" in errors[0]

    def test_missing_lambda_fails_with_actionable_message(self, linter, tmp_path):
        """A Lambda on the allowlist that isn't in the template = someone
        renamed or removed it. The message tells the operator to update
        the allowlist, not to guess."""
        template = self._make_template(tmp_path, "Resources: {}")
        errors = linter._check_allowlisted_lambda(template, "MyDataFn")
        assert len(errors) == 1
        assert "MyDataFn" in errors[0]
        assert "not found" in errors[0].lower()
        assert "out of date" in errors[0].lower()

    def test_missing_template_fails_gracefully(self, linter, tmp_path):
        """The allowlist entry may reference a template that was removed
        — don't crash, produce a clear error."""
        errors = linter._check_allowlisted_lambda(
            tmp_path / "does-not-exist.yaml", "MyDataFn"
        )
        assert len(errors) == 1
        assert "not found" in errors[0].lower()

    def test_cfn_shorthand_tags_parsed(self, linter, tmp_path):
        """CloudFormation shorthand (`!Ref`, `!Sub`) elsewhere in the
        template must not break YAML parsing. The linter defines a
        custom loader that stubs these — regression guard."""
        template = self._make_template(
            tmp_path,
            """
            Resources:
              MyDataFn:
                Type: AWS::Serverless::Function
                Properties:
                  Handler: index.handler
                  Environment:
                    Variables:
                      STACK_NAME: !Ref AWS::StackName
                      BUCKET: !Sub "${AWS::StackName}-bucket"
                  Tags:
                    idp:plane: data
        """,
        )
        assert linter._check_allowlisted_lambda(template, "MyDataFn") == []


@pytest.mark.unit
class TestEndToEnd:
    """Actually invoke the linter script and assert its behavior on the
    real repo templates. This is the guard that catches regressions in
    the templates themselves — if someone drops the tag from OCRFunction
    tomorrow, this test fails."""

    def test_current_repo_passes(self):
        result = subprocess.run(
            [sys.executable, str(LINTER)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, (
            f"Linter failed on current repo templates.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "OK" in result.stdout
        # Must name the count so a reader can eyeball whether it looks right.
        assert "allowlisted data-plane Lambdas" in result.stdout
