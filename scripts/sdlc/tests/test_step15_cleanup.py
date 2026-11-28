"""Step 15 (config prefix mappings) stops its own documents before cleaning up.

The step asserts routing from the tracking row seconds after upload, while most of
its six documents are still queued or running. Its cleanup used to delete their
objects, rows and profile under them, so each one failed at OCRStep and every
nightly reported those failures. These tests pin the cleanup order with fakes:
abort first (every key but the reject-by-design one), wait bounded for each
document to settle, then delete -- keeping the ABORTED row of a document that was
aborted while QUEUED, which is what makes the queue processor refuse its message.
"""

import io
import json

import codebuild_deployment as cd
import pytest

RUN = "cpm-1700000000"
FN = "idp-x-APIRESOLVERSTACK-AbortWorkflowResolverFun-abc"


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_):
        return self._pages


class _World:
    """One fake stack: tracking rows, executions, and an ordered event log."""

    def __init__(self, *, abort_settles=True, started=()):
        self.events = []
        self.rows = {}
        self.executions = {}
        self.abort_settles = abort_settles
        self.started = set(started)

    # --- the routing rows the step asserts on -------------------------------
    def ingest(self, key, metadata):
        prefix = key.rsplit("/", 1)[0] + "/"
        mode = {"bob1": "mapping", "bob2": "metadata", "bob3": "reject"}[
            prefix.split("-")[-1].rstrip("/")
        ]
        tagged = bool(metadata)
        row = {"ConfigMappingPrefix": {"S": prefix}}
        if mode == "reject" and tagged:
            row.update(
                ConfigSource={"S": "rejected"},
                ObjectStatus={"S": "FAILED"},
                ConfigAssignmentError={"S": "conflict"},
            )
        elif mode == "metadata" and tagged:
            row.update(ConfigSource={"S": "metadata"}, ObjectStatus={"S": "QUEUED"})
        else:
            row.update(
                ConfigSource={"S": "prefix-mapping"}, ObjectStatus={"S": "QUEUED"}
            )
        if key in self.started and row["ObjectStatus"]["S"] == "QUEUED":
            arn = f"arn:aws:states:us-east-1:111111111111:execution:sm:{len(self.executions)}"
            row["ObjectStatus"] = {"S": "OCR"}
            row["WorkflowExecutionArn"] = {"S": arn}
            self.executions[arn] = "RUNNING"
        self.rows[key] = row

    # --- what the product's abortWorkflow does ------------------------------
    def abort(self, keys):
        self.events.append(("abort", tuple(keys)))
        if not self.abort_settles:
            return {"message": "Aborted 0 document(s)", "errors": None}
        for key in keys:
            row = self.rows[key]
            arn = (row.get("WorkflowExecutionArn") or {}).get("S")
            if arn:
                self.executions[arn] = "ABORTED"
            row["ObjectStatus"] = {"S": "ABORTED"}
        return {"message": f"Aborted {len(keys)} document(s)", "errors": None}


def _clients(world):
    class S3:
        def put_object(self, Bucket, Key, Body, Metadata=None):
            world.ingest(Key, Metadata)

        def delete_object(self, Bucket, Key):
            world.events.append(("s3_delete", Key))

    class DDB:
        def get_item(self, TableName, Key, **_):
            key = Key["PK"]["S"][len("doc#") :]
            return {"Item": world.rows.get(key)} if TableName == "tracking" else {}

        def delete_item(self, TableName, Key):
            if TableName == "tracking":
                world.events.append(("row_delete", Key["PK"]["S"][len("doc#") :]))
            else:
                world.events.append(("profile_delete", Key["Configuration"]["S"]))

    class CF:
        def get_paginator(self, name):
            assert name == "list_stack_resources"
            return _Paginator(
                [
                    {
                        "StackResourceSummaries": [
                            {
                                "ResourceType": "AWS::DynamoDB::Table",
                                "LogicalResourceId": "ConfigurationTable",
                                "PhysicalResourceId": "config",
                            },
                            {
                                "ResourceType": "AWS::CloudFormation::Stack",
                                "LogicalResourceId": "APIRESOLVERSTACK",
                                "PhysicalResourceId": "arn:nested",
                            },
                        ]
                    }
                ]
            )

        def describe_stack_resource(self, StackName, LogicalResourceId):
            assert (StackName, LogicalResourceId) == (
                "arn:nested",
                "AbortWorkflowResolverFunction",
            )
            return {"StackResourceDetail": {"PhysicalResourceId": FN}}

    class Lambda:
        def invoke(self, FunctionName, InvocationType, Payload):
            assert FunctionName == FN
            event = json.loads(Payload)
            # A direct invoke: no identity, which the resolver admits on IAM.
            assert "identity" not in event
            assert event["info"] == {"fieldName": "abortWorkflow"}
            result = world.abort(event["arguments"]["objectKeys"])
            return {"Payload": io.BytesIO(json.dumps(result).encode())}

    class SFN:
        def describe_execution(self, executionArn):
            return {"status": world.executions[executionArn]}

    return {
        "s3": S3(),
        "dynamodb": DDB(),
        "cloudformation": CF(),
        "lambda": Lambda(),
        "stepfunctions": SFN(),
    }


@pytest.fixture
def run_step(monkeypatch, tmp_path):
    def _run(world, *, timeout=0):
        (tmp_path / "samples").mkdir()
        (tmp_path / "samples" / "lending_package.pdf").write_bytes(b"%PDF")
        monkeypatch.chdir(tmp_path)
        clients = _clients(world)
        monkeypatch.setattr(
            cd.boto3, "client", lambda name, **_: clients[name], raising=True
        )
        monkeypatch.setattr(
            cd.boto3,
            "resource",
            lambda *a, **k: type("R", (), {"Table": lambda self, name: name})(),
        )
        monkeypatch.setattr(
            cd,
            "_stack_outputs",
            lambda _: {"S3InputBucketName": "in", "TrackingTableName": "tracking"},
        )
        monkeypatch.setattr(cd, "run_command", lambda cmd: "")
        monkeypatch.setattr(cd, "regional_config_file", lambda p: p)
        monkeypatch.setattr(cd.time, "time", lambda: 1700000000)
        monkeypatch.setattr(cd.time, "sleep", lambda s: None)
        monkeypatch.setattr(cd, "_CPM_SETTLE_POLL_SECONDS", 0)
        monkeypatch.setattr(cd, "_CPM_SETTLE_TIMEOUT_SECONDS", timeout)

        class Store:
            def __init__(self, table):
                pass

            def put(self, prefix, *a, **k):
                pass

            def delete(self, prefix):
                world.events.append(("mapping_delete", prefix))

        import idp_common.config.prefix_mappings as pm

        monkeypatch.setattr(pm, "PrefixMappingStore", Store)
        return cd.test_step15_config_prefix_mappings("idp-x")

    return _run


def _keys():
    return [
        f"{RUN}-bob{n}/{name}"
        for n in (1, 2, 3)
        for name in ("untagged.pdf", "tagged.pdf")
    ]


REJECTED = f"{RUN}-bob3/tagged.pdf"


@pytest.mark.unit
class TestStep15Cleanup:
    def test_aborts_every_non_reject_document_before_any_delete(self, run_step):
        world = _World(started={f"{RUN}-bob1/untagged.pdf", f"{RUN}-bob2/tagged.pdf"})
        result = run_step(world)
        assert result == {"success": True}

        aborts = [e for e in world.events if e[0] == "abort"]
        assert aborts == [
            ("abort", tuple(k for k in _keys() if k != REJECTED)),
        ]
        first_delete = next(
            i for i, e in enumerate(world.events) if e[0].endswith("_delete")
        )
        assert world.events.index(aborts[0]) < first_delete
        # Every object is still deleted, and the profile last of all.
        assert {e[1] for e in world.events if e[0] == "s3_delete"} == set(_keys())
        assert world.events[-1] == ("profile_delete", f"Config#{RUN}-mapped")

    def test_rows_of_documents_aborted_while_queued_are_kept(self, run_step):
        started = {f"{RUN}-bob1/untagged.pdf", f"{RUN}-bob2/tagged.pdf"}
        world = _World(started=started)
        run_step(world)
        deleted_rows = {e[1] for e in world.events if e[0] == "row_delete"}
        # Started (execution stopped) and reject-by-design rows are deleted; a
        # document aborted while QUEUED keeps the ABORTED row that makes the
        # queue processor refuse its pending message.
        assert deleted_rows == started | {REJECTED}

    def test_unsettled_documents_are_reported_not_failed(self, run_step, capsys):
        world = _World(abort_settles=False)
        result = run_step(world, timeout=0)
        assert result == {"success": True}
        out = capsys.readouterr().out
        assert "5 documents were still in flight" in out
        assert f"{RUN}-bob1/untagged.pdf: status='QUEUED'" in out
        # Cleanup still runs, after the wait.
        assert {e[1] for e in world.events if e[0] == "s3_delete"} == set(_keys())


@pytest.mark.unit
class TestCpmSettle:
    def _clock(self):
        now = [0.0]

        def clock():
            return now[0]

        def sleep(s):
            now[0] += s

        return clock, sleep, now

    def test_wait_is_bounded(self):
        clock, sleep, now = self._clock()
        calls = []

        def observe(key):
            calls.append(key)
            return "OCR", "arn:x", "RUNNING"

        unsettled, keep = cd._cpm_settle(
            ["a", "b"],
            observe=observe,
            timeout=300,
            interval=10,
            clock=clock,
            sleep=sleep,
        )
        assert unsettled == [
            "a: status='OCR' execution='RUNNING'",
            "b: status='OCR' execution='RUNNING'",
        ]
        assert keep == set()
        assert now[0] == 300
        assert len(calls) == 2 * 31

    def test_aborted_row_with_running_execution_is_not_settled(self):
        clock, sleep, now = self._clock()
        states = iter(
            [("ABORTED", "arn:x", "RUNNING"), ("ABORTED", "arn:x", "ABORTED")]
        )
        unsettled, keep = cd._cpm_settle(
            ["a"],
            observe=lambda k: next(states),
            timeout=300,
            interval=10,
            clock=clock,
            sleep=sleep,
        )
        assert unsettled == []
        assert keep == set()
        # The ABORTED row alone did not end the wait; the stopped execution did.
        assert now[0] == 10

    def test_settled_documents_and_kept_rows(self):
        clock, sleep, now = self._clock()
        rows = {
            "queued-aborted": ("ABORTED", None, None),
            "stopped": ("ABORTED", "arn:s", "ABORTED"),
            "done": ("COMPLETED", "arn:d", "SUCCEEDED"),
            "missing": (None, None, None),
        }
        unsettled, keep = cd._cpm_settle(
            list(rows),
            observe=rows.__getitem__,
            timeout=300,
            interval=10,
            clock=clock,
            sleep=sleep,
        )
        assert unsettled == []
        assert keep == {"queued-aborted"}
        assert now[0] == 0  # nothing to wait for, so no sleep

    def test_a_document_waiting_for_review_does_not_hold_the_wait(self):
        # An abort that did not take can leave a document at the review point,
        # where its execution has finished and its row reads HITL_IN_PROGRESS
        # for as long as nobody reviews it. That is not worth the full bound.
        clock, sleep, now = self._clock()
        unsettled, keep = cd._cpm_settle(
            ["held"],
            observe=lambda k: ("HITL_IN_PROGRESS", "arn:h", "SUCCEEDED"),
            timeout=300,
            interval=10,
            clock=clock,
            sleep=sleep,
        )
        assert (unsettled, keep, now[0]) == ([], set(), 0)


@pytest.mark.unit
def test_the_abort_call_is_not_retried_and_outlasts_the_resolver():
    retries = cd._CPM_ABORT_CLIENT_CONFIG.retries or {}
    assert retries.get("total_max_attempts") == 1
    assert (cd._CPM_ABORT_CLIENT_CONFIG.read_timeout or 0) > 60
