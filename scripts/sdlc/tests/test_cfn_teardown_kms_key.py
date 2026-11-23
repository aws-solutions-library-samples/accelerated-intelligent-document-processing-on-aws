# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The CloudFormation teardown's handling of the stack's retained KMS key (#2235).

template.yaml's CustomerManagedEncryptionKey is `DeletionPolicy: Retain`, so a
stack delete leaves it standing and billed. Two places schedule its deletion:
`cleanup_stack`, which reads the key's physical id before this run's own stack
delete and schedules it once the stack is gone, and the Step 0 sweep
`cleanup_stale_idp_kms_keys`, which takes the keys of dead runs whose own
teardown never got that far. The sweep's generic outcome directions (an
enumeration failing is a finding, the quiet path, best-effort per key, the age
gate, the long-lived stacks) are driven from STARTUP_SWEEPS in
test_startup_sweep_outcomes.py; this module pins what is particular to keys.

Every AWS client is a stub; nothing here reaches the network.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

pytestmark = pytest.mark.unit

TEMPLATE = Path(__file__).resolve().parents[3] / "template.yaml"

STACK = "idp-1122-030405"
KEY = "e97cf58a-7231-4e1e-974e-8d06f234ad5f"
OLD = datetime.now(tz=timezone.utc) - timedelta(days=2)


def _error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def _description(stack):
    return f"Encrypts the data of IDP stack {stack}; kept when the stack is deleted"


class _Cfn:
    """describe_stack_resource before the delete, describe_stacks after it."""

    def __init__(self, resource_error=None, stack_after="gone"):
        self.resource_error = resource_error
        self.stack_after = stack_after

    def describe_stack_resource(self, StackName, LogicalResourceId):  # noqa: N803
        assert LogicalResourceId == "CustomerManagedEncryptionKey"
        if self.resource_error:
            raise self.resource_error
        return {"StackResourceDetail": {"PhysicalResourceId": KEY}}

    def describe_stacks(self, StackName):  # noqa: N803
        if self.stack_after == "gone":
            raise _error("ValidationError", "DescribeStacks")
        return {"Stacks": [{"StackName": StackName, "StackStatus": self.stack_after}]}


class _Kms:
    """describe_key / schedule_key_deletion / list_keys over {id: metadata}."""

    def __init__(self, keys, schedule_error=None, page_size=2):
        self.keys = keys
        self.schedule_error = schedule_error
        self.page_size = page_size
        self.scheduled = []

    def describe_key(self, KeyId):  # noqa: N803
        if KeyId not in self.keys:
            raise _error("NotFoundException", "DescribeKey")
        return {"KeyMetadata": {"KeyId": KeyId, **self.keys[KeyId]}}

    def schedule_key_deletion(self, KeyId, PendingWindowInDays):  # noqa: N803
        if self.schedule_error:
            raise self.schedule_error
        self.scheduled.append((KeyId, PendingWindowInDays))
        self.keys[KeyId]["KeyState"] = "PendingDeletion"

    def get_paginator(self, api):
        assert api == "list_keys", api
        ids = list(self.keys)
        pages = [
            {"Keys": [{"KeyId": k} for k in ids[i : i + self.page_size]]}
            for i in range(0, len(ids), self.page_size)
        ]
        return SimpleNamespace(paginate=lambda: pages)


@pytest.fixture
def teardown(cbd, monkeypatch):
    """cleanup_stack with the CLI, the ingestion cancel and the IAM teardown
    stubbed; `teardown.cfn` and `teardown.kms` are the clients boto3 hands out."""
    state = SimpleNamespace(cfn=_Cfn(), kms=_Kms({KEY: {"KeyState": "Enabled"}}))
    deleted = []

    def _client(service, **_kw):
        return {"cloudformation": state.cfn, "kms": state.kms}[service]

    def _run_command(cmd, **_kw):
        deleted.append(cmd)
        return SimpleNamespace(returncode=0, stdout="CREATE_COMPLETE", stderr="")

    monkeypatch.setattr(cbd.boto3, "client", _client)
    monkeypatch.setattr(cbd, "run_command", _run_command)
    monkeypatch.setattr(cbd, "cancel_bedrock_ingestion_jobs", lambda _s: None)
    monkeypatch.setattr(cbd, "cleanup_iam_resources", lambda _s: [])
    state.deleted = deleted
    return state


def test_the_retained_key_is_scheduled_after_the_stack_delete(cbd, teardown):
    findings = cbd.cleanup_stack({"stack_name": STACK})

    assert findings == []
    assert teardown.kms.scheduled == [(KEY, 7)]
    assert any("idp-cli delete" in c for c in teardown.deleted)


def test_no_key_to_schedule_when_a_key_arn_was_supplied_or_the_stack_is_gone(
    cbd, teardown
):
    # CloudFormation answers ValidationError both for a resource the stack does
    # not have (CreateStackKmsKey false) and for a stack that no longer exists.
    teardown.cfn = _Cfn(
        resource_error=_error("ValidationError", "DescribeStackResource")
    )

    assert cbd.cleanup_stack({"stack_name": STACK}) == []
    assert teardown.kms.scheduled == []


def test_a_key_already_pending_deletion_or_gone_is_done(cbd, teardown):
    for keys in ({KEY: {"KeyState": "PendingDeletion"}}, {}):
        teardown.kms = _Kms(keys)
        assert cbd.cleanup_stack({"stack_name": STACK}) == [], keys
        assert teardown.kms.scheduled == [], keys


def test_a_key_that_cannot_be_scheduled_is_a_teardown_finding(cbd, teardown):
    teardown.kms = _Kms(
        {KEY: {"KeyState": "Enabled"}},
        schedule_error=_error("AccessDeniedException", "ScheduleKeyDeletion"),
    )

    findings = cbd.cleanup_stack({"stack_name": STACK})

    assert len(findings) == 1, findings
    assert KEY in findings[0] and "AccessDeniedException" in findings[0]


def test_an_unreadable_key_is_a_finding_and_a_standing_stack_keeps_its_key(
    cbd, teardown
):
    # A refused read leaves whether a key is left behind unknown: a finding.
    teardown.cfn = _Cfn(resource_error=_error("AccessDenied", "DescribeStackResource"))
    findings = cbd.cleanup_stack({"stack_name": STACK})
    assert len(findings) == 1 and "CustomerManagedEncryptionKey" in findings[0]
    assert teardown.kms.scheduled == []

    # A stack whose delete failed still uses its key: not scheduled, and not this
    # key's finding (the Step 0 key sweep takes it once the stack is gone).
    teardown.cfn = _Cfn(stack_after="DELETE_FAILED")
    assert cbd.cleanup_stack({"stack_name": STACK}) == []
    assert teardown.kms.scheduled == []


# ---------------------------------------------------------------------------
# The Step 0 sweep: which keys it attributes to a dead run
# ---------------------------------------------------------------------------


def test_the_sweep_schedules_only_a_dead_runs_key(cbd, monkeypatch):
    """One account holding a key of every kind, paged two to a page.

    Only the dead runs' Enabled and Disabled keys go. A live run's key, a
    long-lived stack's key, a key whose description is not template.yaml's
    sentence exactly, and a dead run's key already pending deletion or
    unavailable all stand.
    """
    dead, live = "idp-0101-000000", "idp-0202-000000-hdls"
    # Listed in this order, two to a page, so the two dead-run keys are on the
    # last page and a sweep that read only the first would schedule nothing.
    keys = {
        "k-live": {"Description": _description(live), "KeyState": "Enabled"},
        "k-exempt": {
            "Description": _description("idp-ux-nightly"),
            "KeyState": "Enabled",
        },
        "k-lookalike": {
            "Description": _description(dead) + " (copy)",
            "KeyState": "Enabled",
        },
        "k-other": {
            "Description": "KMS key for DynamoDB encryption",
            "KeyState": "Enabled",
        },
        "k-pending": {
            "Description": _description("idp-0303-000000"),
            "KeyState": "PendingDeletion",
        },
        # A state the shared helper would still schedule from, so only the
        # sweep's own state rule keeps it.
        "k-unavailable": {
            "Description": _description("idp-0303-000000-govc"),
            "KeyState": "Unavailable",
        },
        "k-dead": {"Description": _description(dead), "KeyState": "Enabled"},
        "k-dead-disabled": {
            "Description": _description(f"{dead}-probe"),
            "KeyState": "Disabled",
        },
    }
    for meta in keys.values():
        meta["CreationDate"] = OLD
    kms = _Kms(keys)
    monkeypatch.setattr(cbd.boto3, "client", lambda *_a, **_k: kms)
    monkeypatch.setattr(cbd, "_live_idp_run_prefixes", lambda: {"idp-0202-000000"})

    findings = cbd.cleanup_stale_idp_kms_keys()

    assert findings == []
    assert sorted(kms.scheduled) == [("k-dead", 7), ("k-dead-disabled", 7)]


def test_the_description_matched_is_the_one_template_yaml_writes():
    """The sweep attributes a key by template.yaml's sentence, so the two must
    not drift: a reworded description would leave every new key unattributed."""
    import codebuild_deployment as cbd

    text = TEMPLATE.read_text(encoding="utf-8")
    block = re.search(
        r"^  CustomerManagedEncryptionKey:\n((?:    .*\n|\n)*)", text, re.MULTILINE
    )
    assert block, "template.yaml no longer declares CustomerManagedEncryptionKey"
    assert re.search(r"^    DeletionPolicy: Retain$", block.group(1), re.MULTILINE)
    described = re.search(r'^      Description: !Sub "(.*)"$', block.group(1), re.M)
    assert described, block.group(1)
    rendered = described.group(1).replace("${AWS::StackName}", STACK)

    match = cbd.STACK_KMS_KEY_DESCRIPTION_RE.fullmatch(rendered)
    assert match and match.group(1) == STACK, rendered
