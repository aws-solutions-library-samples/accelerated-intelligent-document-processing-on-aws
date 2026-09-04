# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What an Annotator receives from ``getConfigVersion``.

An Annotator needs the class list to correct a misclassified section — the write
side of that, ``reextractTestSetDocument``, has always accepted them. Without the
list the dropdown had nothing to offer and the editor fell back to a free-text box,
handing the role least able to know the vocabulary an unconstrained field. A class
no config defines produces a section with no schema and therefore no extracted
fields.

Granting Annotator the configuration in full would have exposed prompts, model ids
and every other setting to the lowest-privilege role, so the resolver reduces the
response instead. These tests pin both halves: the class names get through, and
nothing else does.
"""

import importlib.util
import os
from unittest.mock import patch

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../../..")


@pytest.fixture
def resolver():
    """Load the flat Lambda `index.py` by path, with boto3 stubbed at import."""
    with patch.dict(
        os.environ, {"CONFIGURATION_TABLE_NAME": "cfg", "AWS_REGION": "us-east-1"}
    ):
        with patch("boto3.client"), patch("boto3.resource"):
            spec = importlib.util.spec_from_file_location(
                "configuration_resolver_index",
                os.path.join(
                    _REPO_ROOT,
                    "nested/api-resolvers/src/lambda/configuration_resolver/index.py",
                ),
            )
            if spec is None or spec.loader is None:
                raise ImportError("Could not load configuration_resolver/index.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod


# A config carrying both class shapes seen in the wild, plus settings that must not
# reach an annotator.
_CONFIG = {
    "classes": [
        {
            "$id": "Bank Statement",
            "description": "A monthly account statement",
            "type": "object",
            "properties": {"account_number": {"type": "string"}},
            "x-aws-idp-extraction-prompt": "Extract the account number...",
        },
        {"name": "Payslip", "description": "An employee payslip"},
        {"x-aws-idp-document-type": "W2"},
    ],
    "extraction": {
        "model": "us.anthropic.claude-opus-4-8",
        "system_prompt": "You are an extraction engine...",
    },
    "classification": {"model": "us.amazon.nova-lite-v1:0"},
    "pricing": {"input_per_1k": 0.003},
}


@pytest.mark.unit
class TestClassVocabularyReduction:
    def test_class_names_survive_in_every_shape(self, resolver):
        out = resolver._class_vocabulary_only(_CONFIG)

        names = [
            c.get("$id") or c.get("name") or c.get("x-aws-idp-document-type")
            for c in out["classes"]
        ]
        assert names == ["Bank Statement", "Payslip", "W2"]

    def test_descriptions_survive_because_the_dropdown_shows_them(self, resolver):
        out = resolver._class_vocabulary_only(_CONFIG)

        assert out["classes"][0]["description"] == "A monthly account statement"

    def test_nothing_but_the_vocabulary_gets_through(self, resolver):
        out = resolver._class_vocabulary_only(_CONFIG)

        # No sibling settings.
        assert set(out) == {"classes"}
        assert "extraction" not in out
        assert "classification" not in out
        assert "pricing" not in out
        # And nothing extra on a class either: no schema, no prompt.
        for cls in out["classes"]:
            assert set(cls) <= {"$id", "x-aws-idp-document-type", "name", "description"}
        flattened = repr(out)
        assert "system_prompt" not in flattened
        assert "extraction-prompt" not in flattened
        assert "claude-opus" not in flattened

    def test_an_unrecognised_key_is_dropped_rather_than_passed_through(self, resolver):
        # Allow-list, not deny-list: a config key added later must not reach an
        # annotator by default.
        #
        # The key is named plainly rather than evocatively: an earlier version called
        # it `secret_future_key`, and bandit's B105 pattern-matches key names
        # containing "secret", so a test fixture with no credential in it failed the
        # security gate as a "possible hardcoded password". Renaming beats suppressing
        # — there was nothing to suppress.
        out = resolver._class_vocabulary_only(
            {"classes": [{"$id": "X", "unrecognised_future_key": "dropped"}]}
        )

        assert out["classes"] == [{"$id": "X"}]

    def test_a_config_with_no_classes_reduces_to_nothing(self, resolver):
        assert resolver._class_vocabulary_only({"extraction": {"model": "m"}}) == {}
        assert resolver._class_vocabulary_only({"classes": "not-a-list"}) == {}
        assert resolver._class_vocabulary_only(None) == {}

    def test_a_class_with_no_identifying_key_is_omitted(self, resolver):
        # It could not be offered as an option anyway, and an empty entry would render
        # as a blank row in the dropdown.
        out = resolver._class_vocabulary_only(
            {"classes": [{"type": "object"}, {"$id": "Real"}]}
        )

        assert out["classes"] == [{"$id": "Real"}]


@pytest.mark.unit
class TestWhoGetsTheFullConfiguration:
    def test_annotator_may_run_the_operation_at_all(self, resolver):
        # Without this the request is rejected before the resolver runs, which is what
        # left the dropdown empty.
        assert "Annotator" in resolver._OPERATION_REQUIRED_GROUPS["getConfigVersion"]

    def test_annotator_is_not_entitled_to_the_full_payload(self, resolver):
        assert resolver._FULL_CONFIG_GROUPS == {"Admin", "Author", "Viewer"}
        assert "Annotator" not in resolver._FULL_CONFIG_GROUPS

    def test_a_user_in_both_groups_keeps_the_full_payload(self, resolver):
        # Someone who is an Author AND an Annotator must not be downgraded — the
        # reduction is keyed on the absence of any entitled group, not the presence
        # of Annotator.
        assert resolver._FULL_CONFIG_GROUPS.intersection(["Annotator", "Author"])
        assert not resolver._FULL_CONFIG_GROUPS.intersection(["Annotator"])
