# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the ConfBench Test Set feature API.

Two areas carry real risk and are covered closely:

  * **Authorization.** An ingest moves up to 32.71 GB into the host bucket and
    the delete is destructive, so both must be Admin-only. The JWT authorizer
    proves identity, not authority.
  * **Paginated delete.** A full ConfBench set is ~2,700 objects. An unpaginated
    list_objects_v2 silently orphans everything past the first 1,000 keys — the
    bug this suite exists to prevent (and which the host's own
    test_set_resolver.delete_test_sets still had when this was written).
"""

# The fakes mirror boto3's keyword-only signatures; most args are unused.
# pyright: reportUnusedParameter=false

import json
from typing import Any, Dict, List, Optional

import pytest


def _event(
    method: str,
    path: str,
    groups: Optional[List[str]] = None,
    body: Any = None,
    groups_as_string: bool = False,
) -> Dict[str, Any]:
    """An HTTP API v2 proxy event with Cognito JWT authorizer claims."""
    claims: Dict[str, Any] = {"sub": "user-1"}
    if groups is not None:
        # API Gateway stringifies the group list as "[Admin Author]"; some
        # configurations deliver a real list. The handler must accept both.
        claims["cognito:groups"] = (
            f"[{' '.join(groups)}]" if groups_as_string else groups
        )
    return {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": claims}},
        },
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.fixture
def api(monkeypatch):
    """The API handler with S3/DynamoDB/StepFunctions replaced by fakes."""
    import handler as api_handler

    state: Dict[str, Any] = {
        "objects": {},  # prefix -> list of keys
        "deleted": [],
        "executions": [],
        "job_items": {},
        "jobs_by_test_set": {},
        "ddb_deletes": [],
    }

    class FakeS3:
        def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None, **kw):  # noqa: N803
            keys = state["objects"].get(Prefix, [])
            start = int(ContinuationToken or 0)
            page = keys[start : start + 1000]
            nxt = start + 1000
            truncated = nxt < len(keys)
            resp: Dict[str, Any] = {
                "KeyCount": len(page),
                "Contents": [{"Key": k} for k in page],
                "IsTruncated": truncated,
            }
            if truncated:
                resp["NextContinuationToken"] = str(nxt)
            return resp

        def delete_objects(self, Bucket, Delete):  # noqa: N803
            state["deleted"].extend(o["Key"] for o in Delete["Objects"])
            return {}

    class FakeJobTable:
        def put_item(self, Item):  # noqa: N803
            state["job_items"][Item["jobId"]] = Item

        def get_item(self, Key):  # noqa: N803
            # Also serves the host-record ownership read, which has no jobId.
            item = state["job_items"].get(Key.get("jobId"))
            return {"Item": item} if item else {}

        def update_item(self, **kw):
            return {}

        def query(self, **kw):
            return {"Items": state["jobs_by_test_set"].get("items", [])}

        def scan(self, **kw):
            return {"Items": list(state["job_items"].values())}

        def delete_item(self, Key, **kw):  # noqa: N803
            state["ddb_deletes"].append(Key)

    class FakeDdb:
        def Table(self, name):  # noqa: N802
            return FakeJobTable()

    class FakeSfn:
        def start_execution(self, stateMachineArn, name, input):  # noqa: N803
            state["executions"].append({"name": name, "input": json.loads(input)})
            return {"executionArn": f"arn:aws:states:us-east-1:1:execution:sm:{name}"}

    monkeypatch.setattr(api_handler, "_s3", FakeS3())
    monkeypatch.setattr(api_handler, "_ddb", FakeDdb())
    monkeypatch.setattr(api_handler, "_sfn", FakeSfn())
    return api_handler, state


def _body(resp: Dict[str, Any]) -> Any:
    return json.loads(resp["body"])


@pytest.mark.unit
class TestAuthorization:
    def test_ingest_requires_admin(self, api):
        api_handler, state = api
        resp = api_handler.lambda_handler(
            _event("POST", "/ingest", groups=["Viewer"], body={"tier": "clean"}), None
        )
        assert resp["statusCode"] == 403
        assert not state["executions"], "no job may start without Admin"

    def test_ingest_denied_with_no_groups_at_all(self, api):
        api_handler, state = api
        resp = api_handler.lambda_handler(
            _event("POST", "/ingest", body={"tier": "clean"}), None
        )
        assert resp["statusCode"] == 403
        assert not state["executions"]

    def test_delete_requires_admin(self, api):
        api_handler, state = api
        resp = api_handler.lambda_handler(
            _event("DELETE", "/dataset/confbench-clean", groups=["Author"]), None
        )
        assert resp["statusCode"] == 403
        assert not state["deleted"]

    def test_admin_may_ingest(self, api):
        api_handler, state = api
        resp = api_handler.lambda_handler(
            _event("POST", "/ingest", groups=["Admin"], body={"tier": "clean"}), None
        )
        assert resp["statusCode"] == 202
        assert len(state["executions"]) == 1

    def test_stringified_group_claim_is_accepted(self, api):
        """API Gateway delivers cognito:groups as "[Admin]" — a naive equality
        check against a list would lock out every real admin."""
        api_handler, _ = api
        resp = api_handler.lambda_handler(
            _event(
                "POST",
                "/ingest",
                groups=["Admin", "Author"],
                body={"tier": "clean"},
                groups_as_string=True,
            ),
            None,
        )
        assert resp["statusCode"] == 202

    def test_reads_do_not_require_admin(self, api):
        """The catalog is informational; gating it would stop a viewer from even
        seeing what is deployed."""
        api_handler, _ = api
        resp = api_handler.lambda_handler(
            _event("GET", "/variants", groups=["Viewer"]), None
        )
        assert resp["statusCode"] == 200


@pytest.mark.unit
class TestVariantsRoute:
    def test_returns_catalog_with_deployed_annotation(self, api):
        api_handler, state = api
        state["objects"]["confbench-clean/input/"] = [f"k{i}" for i in range(75)]
        resp = api_handler.lambda_handler(
            _event("GET", "/variants", groups=["Admin"]), None
        )
        body = _body(resp)
        assert body["totalFiles"] == 1346
        assert len(body["variants"]) == 21
        assert body["deployed"]["confbench-clean"] == 75

    def test_deployed_count_paginates(self, api):
        """2,700 objects is past one page; an unpaginated count would under-report
        and the UI would claim a set is smaller than it is."""
        api_handler, state = api
        state["objects"]["confbench/input/"] = [f"k{i}" for i in range(2700)]
        resp = api_handler.lambda_handler(
            _event("GET", "/variants", groups=["Admin"]), None
        )
        assert _body(resp)["deployed"]["confbench"] == 2700


@pytest.mark.unit
class TestIngestRoute:
    def test_rejects_unknown_tier(self, api):
        api_handler, state = api
        resp = api_handler.lambda_handler(
            _event("POST", "/ingest", groups=["Admin"], body={"tier": "enormous"}), None
        )
        assert resp["statusCode"] == 400
        assert "Unknown tier" in _body(resp)["error"]
        assert not state["executions"]

    def test_rejects_unknown_variant(self, api):
        api_handler, state = api
        resp = api_handler.lambda_handler(
            _event(
                "POST", "/ingest", groups=["Admin"], body={"variants": ["archetype99"]}
            ),
            None,
        )
        assert resp["statusCode"] == 400
        assert not state["executions"]

    def test_rejects_empty_selection(self, api):
        api_handler, _ = api
        resp = api_handler.lambda_handler(
            _event("POST", "/ingest", groups=["Admin"], body={"variants": []}), None
        )
        assert resp["statusCode"] == 400

    def test_rejects_malformed_json_body(self, api):
        api_handler, _ = api
        event = _event("POST", "/ingest", groups=["Admin"])
        event["body"] = "{not json"
        resp = api_handler.lambda_handler(event, None)
        assert resp["statusCode"] == 400

    def test_passes_resolved_variants_to_the_state_machine(self, api):
        api_handler, state = api
        api_handler.lambda_handler(
            _event("POST", "/ingest", groups=["Admin"], body={"tier": "light"}), None
        )
        sent = state["executions"][0]["input"]
        assert sent["testSetId"] == "confbench-light"
        assert sent["variants"] == [
            "original",
            "archetype9",
            "archetype4",
            "archetype10",
        ]

    def test_reports_planned_size_in_the_response(self, api):
        """The UI shows this before the confirm; it must match the catalog."""
        api_handler, _ = api
        resp = api_handler.lambda_handler(
            _event("POST", "/ingest", groups=["Admin"], body={"tier": "full"}), None
        )
        body = _body(resp)
        assert body["plannedFiles"] == 1346
        assert body["expectedBytes"] == 32_713_674_359

    def test_refuses_a_concurrent_job_for_the_same_test_set(self, api):
        """Two ingests writing the same keys would race on the row index and
        double-count progress."""
        api_handler, state = api
        state["jobs_by_test_set"]["items"] = [
            {
                "jobId": "existing",
                "jobStatus": "RUNNING",
                "testSetId": "confbench-clean",
            }
        ]
        resp = api_handler.lambda_handler(
            _event("POST", "/ingest", groups=["Admin"], body={"tier": "clean"}), None
        )
        assert resp["statusCode"] == 409
        assert _body(resp)["jobId"] == "existing"
        assert not state["executions"]

    def test_allows_a_new_job_when_the_prior_one_is_terminal(self, api):
        api_handler, state = api
        state["jobs_by_test_set"]["items"] = [
            {"jobId": "old", "jobStatus": "COMPLETED", "testSetId": "confbench-clean"}
        ]
        resp = api_handler.lambda_handler(
            _event("POST", "/ingest", groups=["Admin"], body={"tier": "clean"}), None
        )
        assert resp["statusCode"] == 202


@pytest.mark.unit
class TestJobsRoute:
    def test_get_unknown_job_is_404(self, api):
        api_handler, _ = api
        resp = api_handler.lambda_handler(
            _event(
                "GET", "/jobs/123e4567-e89b-12d3-a456-426614174000", groups=["Admin"]
            ),
            None,
        )
        assert resp["statusCode"] == 404

    def test_malformed_job_id_is_400(self, api):
        api_handler, _ = api
        resp = api_handler.lambda_handler(
            _event("GET", "/jobs/../../etc/passwd", groups=["Admin"]), None
        )
        assert resp["statusCode"] == 400

    def test_list_jobs_is_newest_first(self, api):
        api_handler, state = api
        state["job_items"] = {
            "a": {"jobId": "a", "createdAt": "2026-01-01T00:00:00Z"},
            "b": {"jobId": "b", "createdAt": "2026-06-01T00:00:00Z"},
        }
        resp = api_handler.lambda_handler(
            _event("GET", "/jobs", groups=["Admin"]), None
        )
        assert [j["jobId"] for j in _body(resp)["jobs"]] == ["b", "a"]


@pytest.mark.unit
class TestDeleteRoute:
    def test_refuses_a_test_set_this_extension_does_not_own(self, api):
        """Without this the route is an arbitrary-prefix delete against the
        shared TestSet bucket — it could wipe fake-w2 or a customer's own set."""
        api_handler, state = api
        for victim in ("fake-w2", "realkie-fcc-verified", "", "..", "a/b"):
            resp = api_handler.lambda_handler(
                _event("DELETE", f"/dataset/{victim}", groups=["Admin"]), None
            )
            assert resp["statusCode"] in (400, 404), victim
            assert not state["deleted"], f"deleted objects for {victim!r}"

    def test_deletes_all_pages(self, api):
        """~2,700 objects across three pages. The assertion that matters: every
        key is removed, not just the first 1,000."""
        api_handler, state = api
        keys = [f"confbench/input/doc{i}.pdf" for i in range(2700)]
        state["objects"]["confbench/"] = keys
        resp = api_handler.lambda_handler(
            _event("DELETE", "/dataset/confbench", groups=["Admin"]), None
        )
        assert resp["statusCode"] == 200
        assert _body(resp)["objectsDeleted"] == 2700
        assert sorted(state["deleted"]) == sorted(keys)

    def test_removes_the_host_test_set_record(self, api):
        api_handler, state = api
        state["objects"]["confbench-clean/"] = ["confbench-clean/input/a.pdf"]
        api_handler.lambda_handler(
            _event("DELETE", "/dataset/confbench-clean", groups=["Admin"]), None
        )
        assert state["ddb_deletes"] == [
            {"PK": "testset#confbench-clean", "SK": "metadata"}
        ]

    def test_refuses_while_an_ingest_is_running(self, api):
        api_handler, state = api
        state["jobs_by_test_set"]["items"] = [
            {"jobId": "j", "jobStatus": "RUNNING", "testSetId": "confbench"}
        ]
        resp = api_handler.lambda_handler(
            _event("DELETE", "/dataset/confbench", groups=["Admin"]), None
        )
        assert resp["statusCode"] == 409
        assert not state["deleted"]

    def test_missing_test_set_id_is_400(self, api):
        api_handler, _ = api
        resp = api_handler.lambda_handler(
            _event("DELETE", "/dataset", groups=["Admin"]), None
        )
        assert resp["statusCode"] == 400


@pytest.mark.unit
class TestDispatch:
    def test_unknown_path_is_404(self, api):
        api_handler, _ = api
        resp = api_handler.lambda_handler(
            _event("GET", "/nope", groups=["Admin"]), None
        )
        assert resp["statusCode"] == 404

    def test_all_responses_are_json(self, api):
        api_handler, _ = api
        for method, path in (
            ("GET", "/variants"),
            ("GET", "/jobs"),
            ("GET", "/nope"),
        ):
            resp = api_handler.lambda_handler(
                _event(method, path, groups=["Admin"]), None
            )
            assert resp["headers"]["Content-Type"] == "application/json"
            json.loads(resp["body"])

    def test_decimal_values_serialize(self, api):
        """DynamoDB returns Decimal for every number; json.dumps rejects it
        without the default= hook."""
        from decimal import Decimal

        api_handler, state = api
        state["job_items"] = {
            "a": {
                "jobId": "a",
                "createdAt": "2026-01-01T00:00:00Z",
                "filesUploaded": Decimal(75),
            }
        }
        resp = api_handler.lambda_handler(
            _event("GET", "/jobs", groups=["Admin"]), None
        )
        assert resp["statusCode"] == 200
        assert _body(resp)["jobs"][0]["filesUploaded"] == 75
