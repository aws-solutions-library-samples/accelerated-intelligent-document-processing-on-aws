# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which profiles' routing tables name a review queue (``find_queue_references``).

Against a real ``ConfigurationManager`` over moto, so the profile list, the
revision counters and a pinned read are the manager's own. A few cases that need
many profiles or a failing read use a small stand-in instead.
"""

import threading

import boto3
import pytest
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.models import IDPConfig
from idp_common.config.routing_models import RoutingConfig
from idp_common.config.routing_references import (
    MAX_PROFILES_PER_PAGE,
    QueueReference,
    find_queue_references,
)

pytestmark = pytest.mark.unit

TABLE = "routing-references-config"
BUCKET = "routing-references-config-bucket"
REGION = "us-east-1"


def _rule(rule_id, queue, *, label="", enabled=True):
    return {
        "id": rule_id,
        "label": label,
        "enabled": enabled,
        "conditions": [],
        "actions": [{"type": "review_queue", "queue": queue}] if queue else [],
    }


def _routing(*rules, default_queue=None):
    table = {"rules": list(rules)}
    if default_queue:
        table["default_outcome"] = {
            "actions": [{"type": "review_queue", "queue": default_queue}]
        }
    return {"tables": [table]}


@pytest.fixture
def manager(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)
    with mock_aws():
        boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        configured = ConfigurationManager()
        # Every profile is resolved against this one. It routes nowhere.
        _save(configured, "default", _routing())
        yield configured


def _save(manager, profile, routing):
    manager.save_configuration(
        CONFIG_TYPE_CONFIG, IDPConfig(routing=routing), version=profile
    )


def _counters(manager, profile):
    (found,) = [
        p for p in manager.list_config_versions() if p["versionName"] == profile
    ]
    return found["latestRevision"], found["publishedRevision"]


def _publish(profile, revision):
    """Point the profile at an earlier revision, as a save that was recorded and
    not published leaves it: the newest revision is then not the published one."""
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).update_item(
        Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#{profile}"},
        UpdateExpression="SET PublishedRevision = :r",
        ExpressionAttributeValues={":r": revision},
    )


class TestAReference:
    def test_a_rule_that_names_the_queue(self, manager):
        _save(manager, "lending", _routing(_rule("big", "claims", label="Big loans")))
        latest, published = _counters(manager, "lending")
        assert latest == published

        found, cursor = find_queue_references(manager, "claims")

        assert found == [
            QueueReference(
                profile="lending",
                revision=latest,
                published=True,
                rule_id="big",
                rule_label="Big loans",
            )
        ]
        assert cursor is None

    def test_the_default_outcome_is_a_reference_with_no_rule(self, manager):
        _save(manager, "lending", _routing(default_queue="claims"))

        (found,), _ = find_queue_references(manager, "claims")

        assert (found.rule_id, found.rule_label) == ("", "")

    def test_a_rule_with_no_action_names_no_queue(self, manager):
        """Not reviewed: it refers to no queue, the empty id included."""
        _save(
            manager,
            "lending",
            _routing(_rule("none", ""), _rule("held", "claims")),
        )

        found, _ = find_queue_references(manager, "claims")
        nothing, _ = find_queue_references(manager, "approvals")

        assert [reference.rule_id for reference in found] == ["held"]
        assert nothing == []

    def test_the_queue_is_the_one_of_the_review_queue_action(self, manager):
        """An action of a type this release does not know is not a reference."""
        rule = _rule("mixed", "claims")
        rule["actions"].insert(0, {"type": "notify", "queue": "approvals"})
        _save(manager, "lending", _routing(rule))

        found, _ = find_queue_references(manager, "claims")
        other, _ = find_queue_references(manager, "approvals")

        assert [reference.rule_id for reference in found] == ["mixed"]
        assert other == []

    def test_a_disabled_rule_still_names_the_queue(self, manager):
        _save(manager, "lending", _routing(_rule("off", "claims", enabled=False)))

        (found,), _ = find_queue_references(manager, "claims")

        assert found.rule_id == "off"

    def test_every_rule_that_names_it_in_table_order(self, manager):
        _save(
            manager,
            "lending",
            _routing(
                _rule("first", "claims"),
                _rule("other", "approvals"),
                _rule("second", "claims"),
                default_queue="claims",
            ),
        )

        found, _ = find_queue_references(manager, "claims")

        assert [r.rule_id for r in found] == ["first", "second", ""]

    def test_a_queue_nobody_references(self, manager):
        _save(manager, "lending", _routing(_rule("big", "claims")))

        assert find_queue_references(manager, "approvals") == ([], None)

    def test_a_profile_with_the_built_in_rules_references_the_default_queue(
        self, manager
    ):
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="plain")

        found, _ = find_queue_references(manager, "default")

        assert [(r.profile, r.rule_id) for r in found] == [
            ("plain", "builtin-extraction-failed"),
            ("plain", "builtin-confidence-alerts"),
            ("plain", "builtin-missing-required"),
        ]


class TestTheNewestAndThePublishedRevision:
    def test_a_profile_whose_newest_revision_is_published_is_listed_once(self, manager):
        _save(manager, "lending", _routing(_rule("a", "claims")))
        _save(manager, "lending", _routing(_rule("b", "claims")))
        latest, published = _counters(manager, "lending")
        assert latest == published

        found, _ = find_queue_references(manager, "claims")

        assert [(r.rule_id, r.revision, r.published) for r in found] == [
            ("b", latest, True)
        ]

    def test_both_are_read_when_another_revision_is_published(self, manager):
        """The editor shows the newest; documents run the published one."""
        _save(manager, "lending", _routing(_rule("old", "claims")))
        first, _ = _counters(manager, "lending")
        _save(manager, "lending", _routing(_rule("new", "approvals")))
        latest, _ = _counters(manager, "lending")
        assert latest != first
        _publish("lending", first)

        claims, _ = find_queue_references(manager, "claims")
        approvals, _ = find_queue_references(manager, "approvals")

        assert [(r.rule_id, r.revision, r.published) for r in claims] == [
            ("old", first, True)
        ]
        assert [(r.rule_id, r.revision, r.published) for r in approvals] == [
            ("new", latest, False)
        ]

    def test_a_queue_named_in_both_is_listed_for_each(self, manager):
        _save(manager, "lending", _routing(_rule("old", "claims")))
        first, _ = _counters(manager, "lending")
        _save(manager, "lending", _routing(_rule("new", "claims")))
        latest, _ = _counters(manager, "lending")
        _publish("lending", first)

        found, _ = find_queue_references(manager, "claims")

        # The newest configuration first, then the published revision.
        assert [(r.rule_id, r.revision, r.published) for r in found] == [
            ("new", latest, False),
            ("old", first, True),
        ]


class TestPaging:
    @pytest.fixture
    def five(self, manager):
        for name in ("alpha", "bravo", "charlie", "delta", "echo"):
            queue = "approvals" if name == "charlie" else "claims"
            _save(manager, name, _routing(_rule("r", queue)))
        return manager

    def _profiles(self, found):
        return [r.profile for r in found]

    def test_a_page_covers_the_profiles_after_the_cursor_in_name_order(self, five):
        """Six profiles in name order: alpha, bravo, charlie, default, delta, echo.
        `default` holds no rule and charlie names another queue."""
        first, cursor = find_queue_references(five, "claims", limit=3)
        second, next_cursor = find_queue_references(
            five, "claims", limit=3, cursor=cursor
        )

        assert (self._profiles(first), cursor) == (["alpha", "bravo"], "charlie")
        assert (self._profiles(second), next_cursor) == (["delta", "echo"], None)

    def test_the_last_page_has_no_cursor(self, five):
        found, cursor = find_queue_references(five, "claims", limit=2, cursor="delta")

        assert (self._profiles(found), cursor) == (["echo"], None)

    def test_a_page_with_no_reference_still_has_a_cursor(self, five):
        """A caller pages until there is no cursor, not until a page is empty."""
        found, cursor = find_queue_references(five, "approvals", limit=2)

        assert (found, cursor) == ([], "bravo")

    def test_paging_to_the_end_finds_every_reference(self, five):
        found, cursor = [], None
        while True:
            page, cursor = find_queue_references(five, "claims", limit=2, cursor=cursor)
            found += page
            if cursor is None:
                break

        assert self._profiles(found) == ["alpha", "bravo", "delta", "echo"]

    def test_a_cursor_past_the_last_profile_gives_nothing(self, five):
        assert find_queue_references(five, "claims", cursor="zulu") == ([], None)

    def test_a_limit_below_one_reads_one_profile(self, five):
        found, cursor = find_queue_references(five, "claims", limit=0)

        assert (self._profiles(found), cursor) == (["alpha"], "alpha")

    def test_only_profiles_in_the_callers_scope_are_read(self, five):
        found, cursor = find_queue_references(
            five, "claims", allowed_versions=["bravo", "echo"]
        )

        assert (self._profiles(found), cursor) == (["bravo", "echo"], None)


class _Profiles:
    """A stand-in manager over profiles held in memory."""

    def __init__(self, names, queue="claims", fail=None):
        self.names = list(names)
        self.queue = queue
        self.fail = fail
        self.reads = []
        self.threads = set()

    def list_config_versions(self, raise_errors=False):
        assert raise_errors is True
        return [
            {"versionName": name, "latestRevision": 1, "publishedRevision": 1}
            for name in self.names
        ]

    def get_merged_configuration(self, name, revision=None):
        self.reads.append((name, revision))
        self.threads.add(threading.current_thread().name)
        if name == self.fail:
            raise RuntimeError("the profile could not be read")
        return IDPConfig(
            routing=RoutingConfig.model_validate(_routing(_rule("r", self.queue)))
        )


class TestTheStandIn:
    def test_a_page_never_covers_more_than_the_maximum(self):
        names = [f"p{n:02d}" for n in range(MAX_PROFILES_PER_PAGE + 5)]
        profiles = _Profiles(names)

        found, cursor = find_queue_references(profiles, "claims", limit=1000)

        assert len(found) == MAX_PROFILES_PER_PAGE
        assert cursor == names[MAX_PROFILES_PER_PAGE - 1]
        assert len(profiles.reads) == MAX_PROFILES_PER_PAGE

    def test_the_profiles_are_read_in_a_bounded_pool(self):
        profiles = _Profiles([f"p{n:02d}" for n in range(20)])

        find_queue_references(profiles, "claims", limit=20)

        assert 1 <= len(profiles.threads) <= 8
        assert all(name.startswith("queue-references") for name in profiles.threads)

    def test_the_references_come_back_in_profile_order(self):
        names = [f"p{n:02d}" for n in range(20)]

        found, _ = find_queue_references(_Profiles(reversed(names)), "claims", limit=20)

        assert [r.profile for r in found] == names

    def test_a_profile_that_cannot_be_read_fails_the_call(self):
        """An incomplete list would read as "nothing references this queue"."""
        profiles = _Profiles(["a", "b", "c"], fail="b")

        with pytest.raises(RuntimeError, match="could not be read"):
            find_queue_references(profiles, "claims")

    def test_a_published_revision_is_read_at_that_revision(self):
        profiles = _Profiles(["a"])
        profiles.list_config_versions = lambda raise_errors=False: [
            {"versionName": "a", "latestRevision": 7, "publishedRevision": 4}
        ]

        found, _ = find_queue_references(profiles, "claims")

        assert sorted(profiles.reads, key=str) == [("a", 4), ("a", None)]
        assert [(r.revision, r.published) for r in found] == [(7, False), (4, True)]

    def test_a_profile_with_no_revisions_runs_its_head(self):
        profiles = _Profiles(["a"])
        profiles.list_config_versions = lambda raise_errors=False: [
            {"versionName": "a", "latestRevision": None, "publishedRevision": None}
        ]

        found, _ = find_queue_references(profiles, "claims")

        assert profiles.reads == [("a", None)]
        assert [(r.revision, r.published) for r in found] == [(None, True)]
