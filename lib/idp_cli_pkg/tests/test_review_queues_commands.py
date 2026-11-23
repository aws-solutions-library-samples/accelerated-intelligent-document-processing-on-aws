# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``idp-cli review-queues``: list, create, update and delete production queues.

Each command is a thin call into ``IDPClient.review_queues``. These tests replace
the client and pin three things: what the SDK is asked for (the definition a
file and the options add up to, the region), where output goes (``--json`` is the
only thing on stdout), and that a refusal exits 1 with the server's message.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from idp_cli.cli import cli
from idp_sdk import IDPValidationError
from idp_sdk.models import ReviewQueueDefinition, ReviewQueuePage

pytestmark = pytest.mark.unit

STACK = ["--stack-name", "my-stack"]


def _queue(queue_id="claims", **fields) -> ReviewQueueDefinition:
    values = {
        "id": queue_id,
        "name": queue_id.title(),
        "priority": 50,
        "checklist": [],
        "reject_reasons": [],
        "skip_reasons": [],
        "time_limit_minutes": 120,
        "eligible_annotators": ["a@example.com"],
        "is_default_queue": queue_id == "default",
        "enabled": True,
        "version": 3,
    }
    values.update(fields)
    return ReviewQueueDefinition(**values)


@pytest.fixture
def run():
    """Invoke ``review-queues`` with the SDK client replaced; returns (result, queues, class)."""

    def invoke(*arguments, queues=None, **kwargs):
        queues = queues if queues is not None else MagicMock()
        with patch("idp_sdk.IDPClient") as client_class:
            client_class.return_value.review_queues = queues
            result = CliRunner().invoke(cli, ["review-queues", *arguments], **kwargs)
        return result, queues, client_class

    return invoke


def _stdout(result) -> str:
    return result.stdout


class TestList:
    def test_the_page_is_shown_as_a_table_on_stderr(self, run):
        queues = MagicMock()
        queues.list.return_value = ReviewQueuePage(
            queues=[_queue("claims", priority=80), _queue("default")],
            count=2,
            next_token="token-2",
        )

        result, _, _ = run("list", *STACK, queues=queues)

        assert result.exit_code == 0, result.output
        assert _stdout(result) == ""
        # The table wraps a cell to the terminal's width, so each word is looked
        # for on its own.
        for word in ("claims", "default", "(default)", "80"):
            assert word in result.stderr, word
        assert "--next-token token-2" in result.stderr
        queues.list.assert_called_once_with(limit=25, next_token=None)

    def test_json_is_the_only_thing_on_stdout(self, run):
        queues = MagicMock()
        queues.list.return_value = ReviewQueuePage(
            queues=[_queue("claims")], count=1, next_token=None
        )

        result, _, _ = run("list", *STACK, "--json", queues=queues)

        assert result.exit_code == 0, result.output
        payload = json.loads(_stdout(result))
        assert payload["count"] == 1 and payload["next_token"] is None
        assert payload["queues"][0]["id"] == "claims"
        assert payload["queues"][0]["eligible_annotators"] == ["a@example.com"]
        assert result.stderr == ""

    def test_the_limit_and_the_token_are_passed_on(self, run):
        queues = MagicMock()
        queues.list.return_value = ReviewQueuePage(queues=[], count=0)

        result, _, _ = run(
            "list", *STACK, "--limit", "2", "--next-token", "t1", queues=queues
        )

        assert result.exit_code == 0
        queues.list.assert_called_once_with(limit=2, next_token="t1")
        assert "No review queues on this page" in result.stderr

    @pytest.mark.parametrize("command", ["list", "create", "update", "delete"])
    def test_the_region_reaches_the_client(self, run, command):
        queues = MagicMock()
        queues.list.return_value = ReviewQueuePage(queues=[], count=0)
        queues.create.return_value = _queue()
        queues.update.return_value = _queue()
        queues.delete.return_value = 0
        extra = {
            "list": [],
            "create": ["--id", "claims"],
            "update": ["--id", "claims"],
            "delete": ["--id", "claims", "--yes"],
        }[command]

        result, _, client_class = run(
            command, *STACK, "--region", "eu-central-1", *extra, queues=queues
        )

        assert result.exit_code == 0, result.output
        client_class.assert_called_once_with(
            stack_name="my-stack", region="eu-central-1"
        )

    @pytest.mark.parametrize("command", ["list", "create", "update", "delete"])
    def test_every_command_needs_a_stack(self, run, command):
        result, queues, client_class = run(command)

        assert result.exit_code == 2
        assert "--stack-name" in result.output
        assert client_class.called is False


class TestCreate:
    def test_the_options_make_the_definition(self, run):
        queues = MagicMock()
        queues.create.return_value = _queue("claims", priority=80)

        result, _, _ = run(
            "create",
            *STACK,
            "--id",
            "claims",
            "--name",
            "Claims",
            "--priority",
            "80",
            "--time-limit-minutes",
            "30",
            "--annotator",
            "a@example.com",
            "--annotator",
            "b@example.com",
            "--checklist-item",
            "Checked totals",
            "--reject-reason",
            "Illegible",
            "--skip-reason",
            "Not mine",
            "--disabled",
            queues=queues,
        )

        assert result.exit_code == 0, result.output
        queues.create.assert_called_once_with(
            {
                "id": "claims",
                "name": "Claims",
                "priority": 80,
                "time_limit_minutes": 30,
                "eligible_annotators": ["a@example.com", "b@example.com"],
                "checklist": ["Checked totals"],
                "reject_reasons": ["Illegible"],
                "skip_reasons": ["Not mine"],
                "enabled": False,
            }
        )
        assert "Created the review queue" in result.stderr
        assert _stdout(result) == ""

    def test_only_what_was_given_is_sent(self, run):
        queues = MagicMock()
        queues.create.return_value = _queue()

        run("create", *STACK, "--id", "claims", queues=queues)

        queues.create.assert_called_once_with({"id": "claims"})

    @pytest.mark.parametrize("suffix", [".yaml", ".json"])
    def test_a_file_is_the_definition(self, run, tmp_path, suffix):
        queues = MagicMock()
        queues.create.return_value = _queue()
        definition = {"id": "claims", "name": "Claims", "rejectReasons": ["Illegible"]}
        path = tmp_path / f"queue{suffix}"
        path.write_text(json.dumps(definition))

        result, _, _ = run("create", *STACK, "--file", str(path), queues=queues)

        assert result.exit_code == 0, result.output
        queues.create.assert_called_once_with(definition)

    def test_an_option_overrides_the_file_whichever_way_the_file_spells_it(
        self, run, tmp_path
    ):
        queues = MagicMock()
        queues.create.return_value = _queue()
        path = tmp_path / "queue.yaml"
        path.write_text(
            "id: claims\nname: Claims\nrejectReasons: [Old]\ntimeLimitMinutes: 60\n"
        )

        run(
            "create",
            *STACK,
            "--file",
            str(path),
            "--reject-reason",
            "New",
            "--time-limit-minutes",
            "15",
            queues=queues,
        )

        queues.create.assert_called_once_with(
            {
                "id": "claims",
                "name": "Claims",
                "reject_reasons": ["New"],
                "time_limit_minutes": 15,
            }
        )

    def test_a_definition_with_no_id_is_refused_before_the_client(self, run):
        result, queues, client_class = run("create", *STACK, "--name", "No id")

        assert result.exit_code == 1
        assert "A queue id is required" in result.stderr
        assert client_class.called is False

    @pytest.mark.parametrize("content", ["- a\n- b\n", "just text", ": : :"])
    def test_a_file_that_holds_no_definition_is_refused(self, run, tmp_path, content):
        path = tmp_path / "queue.yaml"
        path.write_text(content)

        result, queues, _ = run("create", *STACK, "--file", str(path))

        assert result.exit_code == 1
        assert str(path) in result.stderr.replace("\n", "")
        assert queues.create.called is False

    def test_a_refusal_exits_one_with_the_servers_message(self, run):
        queues = MagicMock()
        queues.create.side_effect = IDPValidationError(
            "QUEUE_EXISTS: a queue with this id exists"
        )

        result, _, _ = run("create", *STACK, "--id", "claims", queues=queues)

        assert result.exit_code == 1
        assert "QUEUE_EXISTS: a queue with this id exists" in result.stderr
        assert _stdout(result) == ""

    def test_json_prints_the_stored_queue(self, run):
        queues = MagicMock()
        queues.create.return_value = _queue("claims", version=1)

        result, _, _ = run("create", *STACK, "--id", "claims", "--json", queues=queues)

        payload = json.loads(_stdout(result))
        assert (payload["id"], payload["version"]) == ("claims", 1)
        assert result.stderr == ""


class TestUpdate:
    def test_the_definition_and_the_expected_version_are_passed_on(self, run):
        queues = MagicMock()
        queues.update.return_value = _queue("claims", priority=90, version=4)

        result, _, _ = run(
            "update",
            *STACK,
            "--id",
            "claims",
            "--priority",
            "90",
            "--expected-version",
            "3",
            queues=queues,
        )

        assert result.exit_code == 0, result.output
        queues.update.assert_called_once_with(
            {"id": "claims", "priority": 90}, expected_version=3
        )
        assert "Updated the review queue" in result.stderr

    def test_no_expected_version_is_none(self, run):
        queues = MagicMock()
        queues.update.return_value = _queue()

        run("update", *STACK, "--id", "claims", "--enabled", queues=queues)

        queues.update.assert_called_once_with(
            {"id": "claims", "enabled": True}, expected_version=None
        )

    def test_version_zero_is_an_expected_version(self, run):
        queues = MagicMock()
        queues.update.return_value = _queue()

        run(
            "update",
            *STACK,
            "--id",
            "default",
            "--expected-version",
            "0",
            queues=queues,
        )

        assert queues.update.call_args.kwargs == {"expected_version": 0}

    def test_a_queue_that_changed_exits_one(self, run):
        queues = MagicMock()
        queues.update.side_effect = IDPValidationError("QUEUE_CHANGED: changed")

        result, _, _ = run(
            "update", *STACK, "--id", "claims", "--expected-version", "1", queues=queues
        )

        assert result.exit_code == 1
        assert "QUEUE_CHANGED" in result.stderr


class TestDelete:
    def test_it_asks_first_and_no_deletes_nothing(self, run):
        result, queues, _ = run("delete", *STACK, "--id", "claims", input="n\n")

        assert result.exit_code == 0
        assert "Nothing deleted" in result.stderr
        assert queues.delete.called is False

    def test_enter_is_no(self, run):
        """Deleting is the destructive command: the default answer keeps the queue."""
        result, queues, _ = run("delete", *STACK, "--id", "claims", input="\n")

        assert queues.delete.called is False

    def test_yes_deletes_and_says_how_many_documents_moved(self, run):
        queues = MagicMock()
        queues.delete.return_value = 3

        result, _, _ = run("delete", *STACK, "--id", "claims", "--yes", queues=queues)

        assert result.exit_code == 0, result.output
        queues.delete.assert_called_once_with("claims")
        assert "3 document(s) moved to the Default queue" in result.stderr.replace(
            "\n", " "
        )

    def test_a_confirmed_prompt_deletes(self, run):
        queues = MagicMock()
        queues.delete.return_value = 0

        result, _, _ = run(
            "delete", *STACK, "--id", "claims", queues=queues, input="y\n"
        )

        queues.delete.assert_called_once_with("claims")

    def test_the_default_queue_refusal_exits_one(self, run):
        queues = MagicMock()
        queues.delete.side_effect = IDPValidationError(
            "PROTECTED_QUEUE: the Default queue can't be deleted"
        )

        result, _, _ = run("delete", *STACK, "--id", "default", "--yes", queues=queues)

        assert result.exit_code == 1
        assert "PROTECTED_QUEUE" in result.stderr


def test_the_group_is_registered_with_its_four_commands():
    group = cli.commands["review-queues"]

    assert sorted(group.commands) == ["create", "delete", "list", "update"]
    for name, command in group.commands.items():
        options = {param.name for param in command.params}
        assert {"stack_name", "region"} <= options, name
