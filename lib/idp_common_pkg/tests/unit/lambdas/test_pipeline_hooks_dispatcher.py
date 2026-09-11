# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the pipeline-hooks dispatcher Lambda.

The dispatcher is invoked by the unified Step Functions workflow at each
pipeline extension point (postOcr, postClassification, ...). It reads the
active configuration version's `<step>.postHook` list from the
ConfigurationTable and fans out to the registered hook Lambdas.

These tests pin the safety-critical behaviors that keep the host stack inert
when no feature has registered a hook:
- unknown / missing hook point → no-op
- CONFIGURATION_TABLE_NAME unset → no-op (boto3 never touched)
- enabled=False and arn-less entries are filtered out
- hooks are sorted by (order, featureId)
- onError semantics: continue / skip-remaining / fail

...and the document-mutation contract (a hook may return `updatedDocument` to
change what the next workflow step consumes):
- a hook that returns no `updatedDocument` leaves the document byte-identical
- every early return still carries a `document`, so the state machine's
  Apply<Point>HookDocument Pass state always resolves
- guardrails: identity is immutable, `sections` must stay Map-iterable,
  config_version is preserved, oversized/unstorable updates are refused
- a refused or failed update degrades to the passive behavior, never to a
  corrupted pipeline
"""

import importlib
import json
import os
import sys

import pytest

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "../../../../../patterns/unified/src/pipeline_hooks_function",
    )
)


@pytest.fixture(autouse=True)
def _path_setup():
    sys.path.insert(0, LAMBDA_DIR)
    yield
    sys.path.remove(LAMBDA_DIR)
    sys.modules.pop("index", None)


def _reload():
    if "index" in sys.modules:
        del sys.modules["index"]
    return importlib.import_module("index")


def test_unknown_hook_point_is_noop(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    out = mod.lambda_handler({"hookPoint": "postBananas"}, None)
    assert out == {
        "hookPoint": "postBananas",
        "invoked": 0,
        "halt": False,
        # Echoed inbound document (absent here ⇒ None) so the state machine's
        # Apply<Point>HookDocument Pass state always resolves.
        "document": None,
        "results": [],
    }


def test_missing_config_table_is_noop(monkeypatch):
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    mod = _reload()
    out = mod.lambda_handler({"hookPoint": "postOcr"}, None)
    assert out["invoked"] == 0
    assert out["results"] == []


def test_read_hooks_filters_disabled_and_arnless(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()

    class _Table:
        def get_item(self, Key):
            return {
                "Item": {
                    "Configuration": "Config#default",
                    "extraction": {
                        "postHook": [
                            {"featureId": "b", "arn": "arn:b", "order": 50},
                            {"featureId": "skip", "arn": "arn:x", "enabled": False},
                            {"featureId": "noarn"},  # dropped: no arn
                            {"featureId": "a", "arn": "arn:a", "order": 50},
                            {"featureId": "first", "arn": "arn:f", "order": 1},
                        ]
                    },
                }
            }

    hooks = mod._read_hooks_from_config(_Table(), "default", "postExtraction")
    # disabled + arn-less dropped; sorted by (order, featureId)
    assert [h["featureId"] for h in hooks] == ["first", "a", "b"]
    # defaults applied
    assert hooks[0]["onError"] == "continue"
    assert hooks[1]["order"] == 50


def test_pinned_config_version_from_document_is_honored(monkeypatch):
    """A config_version on the document payload pins hook resolution.

    The host's compressed-document wrapper carries config_version (see
    Document.compress), so the dispatcher must resolve hooks from the version
    the document was processed under rather than scanning for IsActive. This is
    what lets a per-document config selection drive its own postRuleValidation
    hook even when a different version is active.
    """
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()

    # Fail the test if the active-version scan is ever consulted: a pinned
    # version must short-circuit before any table scan.
    def _no_scan(table, pinned):
        assert pinned == "pinned-v1.0.0"
        return pinned

    monkeypatch.setattr(mod, "_resolve_active_version", _no_scan)
    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: object())

    seen_versions = []

    def _read(table, version, point):
        seen_versions.append(version)
        return [{"featureId": "f", "arn": "arn:f", "order": 1, "onError": "continue"}]

    monkeypatch.setattr(mod, "_read_hooks_from_config", _read)
    monkeypatch.setattr(
        mod,
        "_invoke_hook",
        lambda h, p: {"featureId": "f", "arn": "arn:f", "ok": True, "result": None},
    )

    out = mod.lambda_handler(
        {
            "hookPoint": "postRuleValidation",
            "document": {"compressed": True, "config_version": "pinned-v1.0.0"},
        },
        None,
    )

    assert out["configVersion"] == "pinned-v1.0.0"
    assert seen_versions == ["pinned-v1.0.0"]


def test_onerror_fail_raises(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    hook = {"featureId": "f", "arn": "arn:f", "order": 1, "onError": "fail"}
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: [hook])
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "default")
    monkeypatch.setattr(
        mod,
        "_invoke_hook",
        lambda h, p: {"featureId": "f", "arn": "arn:f", "ok": False, "error": "boom"},
    )

    # Patch the resource so .Table() returns a sentinel; dispatch path doesn't
    # touch it beyond passing it through to the patched readers above.
    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: object())

    with pytest.raises(RuntimeError, match="onError=fail"):
        mod.lambda_handler({"hookPoint": "postExtraction", "document": {}}, None)


def test_onerror_skip_remaining_stops_after_failure(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    hooks = [
        {"featureId": "f1", "arn": "arn:1", "order": 1, "onError": "skip-remaining"},
        {"featureId": "f2", "arn": "arn:2", "order": 2, "onError": "continue"},
    ]
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: hooks)
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "default")
    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: object())
    monkeypatch.setattr(
        mod,
        "_invoke_hook",
        lambda h, p: {
            "featureId": h["featureId"],
            "arn": h["arn"],
            "ok": False,
            "error": "boom",
        },
    )

    out = mod.lambda_handler({"hookPoint": "postExtraction", "document": {}}, None)
    # only the first hook ran before skip-remaining halted the loop
    assert out["invoked"] == 1
    assert out["results"][0]["featureId"] == "f1"


def test_preprocessing_reads_single_flat_hook(monkeypatch):
    """The preprocessing point reads a SINGLE inline hook — arn/args live
    directly on the `preprocessing` section (no preHook list)."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()

    class _Table:
        def get_item(self, Key):
            return {
                "Item": {
                    "Configuration": "Config#default",
                    "preprocessing": {
                        "enabled": True,
                        "featureId": "pii",
                        "arn": "arn:pii",
                        "onError": "fail",
                    },
                }
            }

    hooks = mod._read_hooks_from_config(_Table(), "default", "preprocessing")
    assert len(hooks) == 1
    assert hooks[0]["featureId"] == "pii"
    assert hooks[0]["arn"] == "arn:pii"
    assert hooks[0]["onError"] == "fail"


def test_preprocessing_disabled_or_arnless_yields_no_hook(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()

    class _Disabled:
        def get_item(self, Key):
            return {
                "Item": {
                    "Configuration": "Config#d",
                    "preprocessing": {"enabled": False, "arn": "arn:x"},
                }
            }

    class _NoArn:
        def get_item(self, Key):
            return {
                "Item": {
                    "Configuration": "Config#d",
                    "preprocessing": {"enabled": True},
                }
            }

    assert mod._read_hooks_from_config(_Disabled(), "d", "preprocessing") == []
    assert mod._read_hooks_from_config(_NoArn(), "d", "preprocessing") == []


def test_hook_args_parsed_and_passed_in_payload(monkeypatch):
    """Generic hook args (list of {key,value}) are parsed from the flat
    preprocessing section and delivered as both `args` and `argsMap`."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()

    class _Table:
        def get_item(self, Key):
            return {
                "Item": {
                    "Configuration": "Config#default",
                    "preprocessing": {
                        "enabled": True,
                        "featureId": "pii",
                        "arn": "arn:pii",
                        "args": [
                            {"key": "mode", "value": "redactcopy_and_stop"},
                            {"key": "store_mapping", "value": "true"},
                            {"not_a_key": "x"},  # dropped: no key
                        ],
                    },
                }
            }

    hooks = mod._read_hooks_from_config(_Table(), "default", "preprocessing")
    assert hooks[0]["args"] == [
        {"key": "mode", "value": "redactcopy_and_stop"},
        {"key": "store_mapping", "value": "true"},
    ]

    # end-to-end: the invoke payload carries args + argsMap
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: hooks)
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "default")
    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: object())
    seen = {}

    def _capture(h, payload):
        seen.update(payload)
        return {
            "featureId": "pii",
            "arn": "arn:pii",
            "ok": True,
            "result": {"halt": False},
        }

    monkeypatch.setattr(mod, "_invoke_hook", _capture)
    mod.lambda_handler({"hookPoint": "preprocessing", "document": {}}, None)
    assert seen["argsMap"] == {"mode": "redactcopy_and_stop", "store_mapping": "true"}
    assert {"key": "mode", "value": "redactcopy_and_stop"} in seen["args"]


def test_halt_aggregated_from_hook_result(monkeypatch):
    """A successful hook returning result.halt=true surfaces top-level halt."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    hook = {"featureId": "pii", "arn": "arn:pii", "order": 1, "onError": "continue"}
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: [hook])
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "default")
    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: object())
    monkeypatch.setattr(
        mod,
        "_invoke_hook",
        lambda h, p: {
            "featureId": "pii",
            "arn": "arn:pii",
            "ok": True,
            "result": {"halt": True, "redactedKey": "_pii_redacted/x.pdf"},
        },
    )
    out = mod.lambda_handler({"hookPoint": "preprocessing", "document": {}}, None)
    assert out["halt"] is True
    assert out["invoked"] == 1


def test_halt_false_when_no_hook_requests_it(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    hook = {"featureId": "pii", "arn": "arn:pii", "order": 1, "onError": "continue"}
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: [hook])
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "default")
    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: object())
    monkeypatch.setattr(
        mod,
        "_invoke_hook",
        lambda h, p: {
            "featureId": "pii",
            "arn": "arn:pii",
            "ok": True,
            "result": {"halt": False, "redactedKey": "_pii_redacted/x.pdf"},
        },
    )
    out = mod.lambda_handler({"hookPoint": "preprocessing", "document": {}}, None)
    assert out["halt"] is False


def test_no_hooks_registered_returns_halt_false(monkeypatch):
    """Backward-compat: no preprocessing hook registered ⇒ halt=false so the
    workflow Choice defaults to normal routing."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: [])
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "default")
    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: object())
    out = mod.lambda_handler({"hookPoint": "preprocessing", "document": {}}, None)
    assert out["invoked"] == 0
    assert out["halt"] is False


def test_preprocessing_sets_visible_status(monkeypatch):
    """When a preprocessing hook WILL run, the dispatcher flips the doc row's
    ObjectStatus to PREPROCESSING (best-effort UI step visibility)."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("TRACKING_TABLE", "TrackTable")
    mod = _reload()
    hook = {"featureId": "pii", "arn": "arn:pii", "order": 1, "onError": "continue"}
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: [hook])
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "default")
    monkeypatch.setattr(
        mod,
        "_invoke_hook",
        lambda h, p: {"featureId": "pii", "arn": "arn:pii", "ok": True, "result": {}},
    )
    updates = {}

    class _Table:
        def update_item(self, **kw):
            updates.update(kw)

    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: _Table())
    mod.lambda_handler(
        {"hookPoint": "preprocessing", "document": {"document_id": "w2.pdf"}}, None
    )
    assert updates["Key"] == {"PK": "doc#w2.pdf", "SK": "none"}
    assert updates["ExpressionAttributeValues"] == {":s": "PREPROCESSING"}
    # Conditional write: never resurrect a deleted/missing row.
    assert "attribute_exists" in updates["ConditionExpression"]


def test_preprocessing_status_not_set_when_no_hooks(monkeypatch):
    """No hook registered ⇒ no status write (host stays inert)."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("TRACKING_TABLE", "TrackTable")
    mod = _reload()
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: [])
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "default")
    called = []
    monkeypatch.setattr(mod, "_set_preprocessing_status", lambda d: called.append(d))
    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: object())
    mod.lambda_handler(
        {"hookPoint": "preprocessing", "document": {"document_id": "w2.pdf"}}, None
    )
    assert called == []


def test_preprocessing_status_failure_never_breaks_dispatch(monkeypatch):
    """A DDB error while writing the status is swallowed; the hook still runs."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("TRACKING_TABLE", "TrackTable")
    mod = _reload()
    hook = {"featureId": "pii", "arn": "arn:pii", "order": 1, "onError": "continue"}
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: [hook])
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "default")
    monkeypatch.setattr(
        mod,
        "_invoke_hook",
        lambda h, p: {"featureId": "pii", "arn": "arn:pii", "ok": True, "result": {}},
    )

    class _Boom:
        def update_item(self, **kw):
            raise RuntimeError("ddb down")

    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: _Boom())
    out = mod.lambda_handler(
        {"hookPoint": "preprocessing", "document": {"document_id": "w2.pdf"}}, None
    )
    assert out["invoked"] == 1


def test_post_step_points_do_not_touch_status(monkeypatch):
    """Only the preprocessing point writes the visible status (post-step points
    already have their own per-step statuses set by the step functions)."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("TRACKING_TABLE", "TrackTable")
    mod = _reload()
    hook = {"featureId": "f", "arn": "arn:f", "order": 1, "onError": "continue"}
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: [hook])
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "default")
    called = []
    monkeypatch.setattr(mod, "_set_preprocessing_status", lambda d: called.append(d))
    monkeypatch.setattr(
        mod,
        "_invoke_hook",
        lambda h, p: {"featureId": "f", "arn": "arn:f", "ok": True, "result": {}},
    )
    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: object())
    mod.lambda_handler(
        {"hookPoint": "postExtraction", "document": {"document_id": "w2.pdf"}}, None
    )
    assert called == []


# ---------------------------------------------------------------------------
# Document mutation: a hook may return `updatedDocument` to change what the
# NEXT workflow step consumes. The dispatcher always returns a `document`, so
# the state machine's Apply<Point>HookDocument Pass state has a stable path.
# ---------------------------------------------------------------------------


def _mutation_env(monkeypatch, mod, hooks, invoke):
    """Wire the common dispatch fakes for a mutation test."""
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: hooks)
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "default")
    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: object())
    monkeypatch.setattr(mod, "_invoke_hook", invoke)


def _hook(feature_id="f", **over):
    h = {
        "featureId": feature_id,
        "arn": f"arn:{feature_id}",
        "order": 1,
        "onError": "continue",
        "allowDocumentUpdate": True,
    }
    h.update(over)
    return h


def _ok(result, feature_id="f"):
    return {
        "featureId": feature_id,
        "arn": f"arn:{feature_id}",
        "ok": True,
        "result": result,
    }


def test_document_echoed_when_hook_returns_no_update(monkeypatch):
    """The historical read-only contract: a hook returning arbitrary JSON leaves
    the document byte-identical, so the Apply Pass state is a no-op copy."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    doc = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/a.json",
        "document_id": "w2.pdf",
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok({"documentId": "w2.pdf", "status": "APPROVED"}),
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": doc}, None)
    assert out["document"] == doc
    assert out["documentUpdatedBy"] == []
    assert "documentUpdated" not in out["results"][0]


def test_no_hooks_registered_still_returns_document(monkeypatch):
    """Backward-compat: with nothing registered the dispatcher echoes the
    document, so ApplyXHookDocument resolves even on an inert stack."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    doc = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/a.json",
        "document_id": "w2.pdf",
    }
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: [])
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "default")
    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: object())
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": doc}, None)
    assert out["document"] == doc
    assert out["invoked"] == 0


def test_unknown_point_and_missing_table_still_return_document(monkeypatch):
    """Every early return carries the document — the Apply state reads it
    unconditionally, exactly like the halt flag."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    doc = {"document_id": "w2.pdf"}
    out = mod.lambda_handler({"hookPoint": "postBananas", "document": doc}, None)
    assert out["document"] == doc

    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    mod = _reload()
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": doc}, None)
    assert out["document"] == doc

    # No version resolvable is the third early return.
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: None)
    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: object())
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": doc}, None)
    assert out["document"] == doc


def test_compressed_reference_update_passes_through(monkeypatch):
    """A hook that wrote the document itself returns a compressed reference; the
    dispatcher validates and forwards it without touching S3."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/old.json",
        "document_id": "w2.pdf",
        "sections": ["1"],
        "config_version": "v1",
    }
    new_ref = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/new.json",
        "document_id": "w2.pdf",
        "sections": ["1", "2"],
        "num_pages": 3,
        "config_version": "v1",
    }
    puts = []
    monkeypatch.setattr(mod._s3, "put_object", lambda **kw: puts.append(kw))
    _mutation_env(
        monkeypatch, mod, [_hook()], lambda h, p: _ok({"updatedDocument": new_ref})
    )

    out = mod.lambda_handler(
        {"hookPoint": "postClassification", "document": inbound}, None
    )
    assert out["document"] == new_ref
    assert out["documentUpdatedBy"] == ["f"]
    assert out["results"][0]["documentUpdated"] is True
    # The bulky document is stripped from the recorded result (SFN history size).
    assert "updatedDocument" not in out["results"][0]["result"]
    assert puts == []  # hook already stored it


def test_inline_document_update_is_compressed_to_working_bucket(monkeypatch):
    """An inline dict is spilled to S3 in the same wrapper shape the step
    Lambdas produce, so the next step's load_document() resolves it normally."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("WORKING_BUCKET", "wb")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/old.json",
        "document_id": "w2.pdf",
    }
    inline = {
        "id": "w2.pdf",
        "input_key": "w2.pdf",
        "num_pages": 2,
        "status": "CLASSIFYING",
        "sections": [
            {"section_id": "1", "classification": "W2"},
            {"section_id": "2", "classification": "Invoice"},
        ],
        "config_version": "v1",
    }
    puts = []
    monkeypatch.setattr(mod._s3, "put_object", lambda **kw: puts.append(kw) or {})
    _mutation_env(
        monkeypatch, mod, [_hook()], lambda h, p: _ok({"updatedDocument": inline})
    )

    out = mod.lambda_handler(
        {"hookPoint": "postClassification", "document": inbound}, None
    )
    ref = out["document"]
    assert ref["compressed"] is True
    assert ref["s3_uri"].startswith("s3://wb/compressed_documents/w2.pdf/")
    assert ref["document_id"] == "w2.pdf"
    assert ref["num_pages"] == 2
    # Section IDs only — the ProcessSections Map iterates this list directly.
    assert ref["sections"] == ["1", "2"]
    assert len(puts) == 1
    assert puts[0]["Bucket"] == "wb"
    assert (
        json.loads(puts[0]["Body"].decode("utf-8"))["sections"][1]["classification"]
        == "Invoice"
    )


def test_inline_update_rejected_when_working_bucket_unset(monkeypatch):
    """No WORKING_BUCKET ⇒ refuse the update and keep the inbound document
    rather than handing the next step an unresolvable payload."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.delenv("WORKING_BUCKET", raising=False)
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/old.json",
        "document_id": "w2.pdf",
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok({"updatedDocument": {"id": "w2.pdf", "num_pages": 1}}),
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert out["document"] == inbound
    assert "WORKING_BUCKET" in out["results"][0]["documentUpdateRejected"]


def test_identity_change_is_rejected(monkeypatch):
    """A hook may not repoint the document's identity: the tracking-table row and
    output prefixes are keyed off it. Compare across wrapper/full shapes too
    (compressed `document_id` vs full `id`)."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("WORKING_BUCKET", "wb")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/o.json",
        "document_id": "w2.pdf",
    }
    puts = []
    monkeypatch.setattr(mod._s3, "put_object", lambda **kw: puts.append(kw) or {})
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok({"updatedDocument": {"id": "evil.pdf", "num_pages": 1}}),
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert out["document"] == inbound
    assert "identity changed" in out["results"][0]["documentUpdateRejected"]
    assert puts == []  # rejected before any S3 write


def test_immutable_bucket_field_change_is_rejected(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("WORKING_BUCKET", "wb")
    mod = _reload()
    inbound = {"id": "w2.pdf", "output_bucket": "real-out", "num_pages": 1}
    monkeypatch.setattr(mod._s3, "put_object", lambda **kw: {})
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {
                "updatedDocument": {
                    "id": "w2.pdf",
                    "output_bucket": "attacker",
                    "num_pages": 1,
                }
            }
        ),
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert out["document"] == inbound
    assert "output_bucket" in out["results"][0]["documentUpdateRejected"]


def test_malformed_sections_in_compressed_ref_is_rejected(monkeypatch):
    """`sections` must be a list of strings — the Map state's ItemsPath reads it
    directly, so a bad value would fail the whole execution, not just the hook."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/o.json",
        "document_id": "w2.pdf",
    }
    bad = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/n.json",
        "document_id": "w2.pdf",
        "sections": [{"section_id": "1"}],  # objects, not id strings
    }
    _mutation_env(
        monkeypatch, mod, [_hook()], lambda h, p: _ok({"updatedDocument": bad})
    )
    out = mod.lambda_handler(
        {"hookPoint": "postClassification", "document": inbound}, None
    )
    assert out["document"] == inbound
    assert "sections" in out["results"][0]["documentUpdateRejected"]


def test_bad_s3_uri_in_compressed_ref_is_rejected(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/o.json",
        "document_id": "w2.pdf",
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "/tmp/x",  # nosec B108 - fixture string in a test event payload, never opened
                    "document_id": "w2.pdf",
                }
            }
        ),
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert out["document"] == inbound
    assert "s3_uri" in out["results"][0]["documentUpdateRejected"]


def test_non_object_updated_document_is_rejected(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {"id": "w2.pdf"}
    for bad in ("a string", [], 42, {}, None):
        _mutation_env(
            monkeypatch, mod, [_hook()], lambda h, p, b=bad: _ok({"updatedDocument": b})
        )
        out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
        assert out["document"] == inbound, f"bad value {bad!r} was accepted"
        assert "non-empty object" in out["results"][0]["documentUpdateRejected"]


def test_config_version_is_restored_if_hook_changes_it(monkeypatch):
    """config_version drives hook resolution for the rest of the pipeline, so a
    hook cannot silently repoint it — the inbound value is restored while the
    hook's real intent (the content change) is honored."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/o.json",
        "document_id": "w2.pdf",
        "config_version": "pinned-v1",
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://wb/compressed_documents/w2.pdf/n.json",
                    "document_id": "w2.pdf",
                    "config_version": "other-v9",
                }
            }
        ),
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert out["document"]["config_version"] == "pinned-v1"
    assert (
        out["document"]["s3_uri"] == "s3://wb/compressed_documents/w2.pdf/n.json"
    )  # content change kept


def test_allow_document_update_false_pins_hook_to_observe_only(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/o.json",
        "document_id": "w2.pdf",
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook(allowDocumentUpdate=False)],
        lambda h, p: _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://wb/compressed_documents/w2.pdf/n.json",
                    "document_id": "w2.pdf",
                }
            }
        ),
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert out["document"] == inbound
    assert "allowDocumentUpdate=false" in out["results"][0]["documentUpdateRejected"]


def test_allow_document_update_defaults_true_in_normalize(monkeypatch):
    """Configs written before this feature have no allowDocumentUpdate key; they
    default to permitted (a registered hook is already admin-approved)."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()

    class _Table:
        def get_item(self, Key):
            return {
                "Item": {
                    "Configuration": "Config#default",
                    "extraction": {
                        "postHook": [{"featureId": "legacy", "arn": "arn:l"}]
                    },
                }
            }

    hooks = mod._read_hooks_from_config(_Table(), "default", "postExtraction")
    assert hooks[0]["allowDocumentUpdate"] is True

    class _Off:
        def get_item(self, Key):
            return {
                "Item": {
                    "Configuration": "Config#default",
                    "extraction": {
                        "postHook": [
                            {
                                "featureId": "ro",
                                "arn": "arn:r",
                                "allowDocumentUpdate": False,
                            }
                        ]
                    },
                }
            }

    assert (
        mod._read_hooks_from_config(_Off(), "default", "postExtraction")[0][
            "allowDocumentUpdate"
        ]
        is False
    )


def test_chained_hooks_see_previous_hook_output(monkeypatch):
    """Hooks at the same point compose: hook #2 receives hook #1's document, not
    the original. Without threading, later hooks would silently clobber earlier
    ones."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/v0.json",
        "document_id": "w2.pdf",
    }
    seen = []

    def _invoke(h, payload):
        seen.append(payload["document"]["s3_uri"])
        n = h["featureId"]
        return _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": f"s3://wb/compressed_documents/w2.pdf/{n}.json",
                    "document_id": "w2.pdf",
                }
            },
            feature_id=n,
        )

    _mutation_env(monkeypatch, mod, [_hook("h1"), _hook("h2", order=2)], _invoke)
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert seen == [
        "s3://wb/compressed_documents/w2.pdf/v0.json",
        "s3://wb/compressed_documents/w2.pdf/h1.json",
    ]
    assert out["document"]["s3_uri"] == "s3://wb/compressed_documents/w2.pdf/h2.json"
    assert out["documentUpdatedBy"] == ["h1", "h2"]


def test_rejected_update_does_not_break_the_chain(monkeypatch):
    """Hook #1's update is refused, hook #2's is accepted — #2 must have received
    the still-valid inbound document, and the refusal is visible per-hook."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/v0.json",
        "document_id": "w2.pdf",
    }
    seen = []

    def _invoke(h, payload):
        seen.append(payload["document"]["s3_uri"])
        if h["featureId"] == "bad":
            return _ok(
                {"updatedDocument": {"id": "somethingelse.pdf"}}, feature_id="bad"
            )
        return _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://wb/compressed_documents/w2.pdf/good.json",
                    "document_id": "w2.pdf",
                }
            },
            feature_id="good",
        )

    _mutation_env(monkeypatch, mod, [_hook("bad"), _hook("good", order=2)], _invoke)
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert seen == [
        "s3://wb/compressed_documents/w2.pdf/v0.json",
        "s3://wb/compressed_documents/w2.pdf/v0.json",
    ]
    assert out["document"]["s3_uri"] == "s3://wb/compressed_documents/w2.pdf/good.json"
    assert out["documentUpdatedBy"] == ["good"]
    assert "documentUpdateRejected" in out["results"][0]


def test_failed_hook_document_is_ignored(monkeypatch):
    """A hook that errored (ok=False) never mutates the document, even if its
    error payload happens to contain an updatedDocument key."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/v0.json",
        "document_id": "w2.pdf",
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: {
            "featureId": "f",
            "arn": "arn:f",
            "ok": False,
            "error": {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://wb/compressed_documents/w2.pdf/x.json",
                }
            },
        },
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert out["document"] == inbound
    assert out["documentUpdatedBy"] == []


def test_halt_and_document_update_coexist(monkeypatch):
    """A preprocessing hook can both rewrite the document and halt; the halt
    Choice reads the same stable path as before."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/v0.json",
        "document_id": "w2.pdf",
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {
                "halt": True,
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://wb/compressed_documents/w2.pdf/red.json",
                    "document_id": "w2.pdf",
                },
            }
        ),
    )
    out = mod.lambda_handler({"hookPoint": "preprocessing", "document": inbound}, None)
    assert out["halt"] is True
    assert out["document"]["s3_uri"] == "s3://wb/compressed_documents/w2.pdf/red.json"


def test_oversized_inline_document_is_rejected(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("WORKING_BUCKET", "wb")
    mod = _reload()
    inbound = {"id": "w2.pdf"}
    huge = {"id": "w2.pdf", "blob": "x" * (mod._MAX_INLINE_DOC_BYTES + 10)}
    puts = []
    monkeypatch.setattr(mod._s3, "put_object", lambda **kw: puts.append(kw) or {})
    _mutation_env(
        monkeypatch, mod, [_hook()], lambda h, p: _ok({"updatedDocument": huge})
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert out["document"] == inbound
    assert "over the" in out["results"][0]["documentUpdateRejected"]
    assert puts == []


def test_s3_put_failure_degrades_to_passive(monkeypatch):
    """A working-bucket write failure must not fail the workflow — the document
    falls back to the pre-hook value, matching the read-only behavior."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("WORKING_BUCKET", "wb")
    mod = _reload()
    inbound = {"id": "w2.pdf", "num_pages": 1}

    def _boom(**kw):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(mod._s3, "put_object", _boom)
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok({"updatedDocument": {"id": "w2.pdf", "num_pages": 9}}),
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert out["document"] == inbound
    assert "working bucket" in out["results"][0]["documentUpdateRejected"]


def test_workflow_control_fields_survive_a_hook_round_trip(monkeypatch):
    """`use_bda` / `bda_project_arn` are injected onto the document payload by
    the queue processor for the state machine's JSONPath reads, but are NOT
    Document model fields — so a hook that does the natural
    load -> mutate -> return through idp_common drops them.

    Regression: dropping `use_bda` failed the execution outright at
    RouteByProcessingMode with "Invalid path '$.document.use_bda'". The
    dispatcher must carry them forward onto the hook's document.
    """
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/v0.json",
        "document_id": "w2.pdf",
        "use_bda": False,
        "bda_project_arn": "arn:aws:bedrock:us-west-2:1:data-automation-project/p",
    }
    # A faithful stand-in for idp_common's round-trip: control fields absent.
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://wb/compressed_documents/w2.pdf/v1.json",
                    "document_id": "w2.pdf",
                }
            }
        ),
    )
    out = mod.lambda_handler({"hookPoint": "preprocessing", "document": inbound}, None)
    assert (
        out["document"]["s3_uri"] == "s3://wb/compressed_documents/w2.pdf/v1.json"
    )  # mutation applied
    assert out["document"]["use_bda"] is False
    assert out["document"]["bda_project_arn"] == inbound["bda_project_arn"]


def test_hook_may_deliberately_change_a_control_field(monkeypatch):
    """Back-filling must not clobber a value the hook set on purpose — a
    preprocessing hook rerouting a document to the pipeline branch is a
    legitimate use of this feature."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/v0.json",
        "document_id": "w2.pdf",
        "use_bda": True,
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://wb/compressed_documents/w2.pdf/v1.json",
                    "document_id": "w2.pdf",
                    "use_bda": False,
                }
            }
        ),
    )
    out = mod.lambda_handler({"hookPoint": "preprocessing", "document": inbound}, None)
    assert out["document"]["use_bda"] is False


def test_control_fields_carried_across_a_hook_chain(monkeypatch):
    """Each hook in the chain drops them; each must get them back, or a later
    hook's document reaches the workflow without them."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/v0.json",
        "document_id": "w2.pdf",
        "use_bda": False,
    }
    seen = []

    def _invoke(h, payload):
        seen.append(payload["document"].get("use_bda"))
        return _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": f"s3://wb/compressed_documents/w2.pdf/{h['featureId']}.json",
                    "document_id": "w2.pdf",
                }
            },
            feature_id=h["featureId"],
        )

    _mutation_env(monkeypatch, mod, [_hook("h1"), _hook("h2", order=2)], _invoke)
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    # Hook #2 saw the control field even though hook #1's document lacked it.
    assert seen == [False, False]
    assert out["document"]["use_bda"] is False


def test_inline_document_update_preserves_control_fields(monkeypatch):
    """The inline path rebuilds the wrapper from scratch, so it needs the same
    back-fill as the compressed-reference path."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("WORKING_BUCKET", "wb")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/v0.json",
        "document_id": "w2.pdf",
        "use_bda": False,
    }
    monkeypatch.setattr(mod._s3, "put_object", lambda **kw: {})
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {"updatedDocument": {"id": "w2.pdf", "num_pages": 1, "sections": []}}
        ),
    )
    out = mod.lambda_handler({"hookPoint": "preprocessing", "document": inbound}, None)
    assert out["document"]["compressed"] is True
    assert out["document"]["use_bda"] is False


# ---------------------------------------------------------------------------
# Compressed-reference URI constraints. Document.decompress() parses the URI
# but DISCARDS its bucket, reading the key against the consumer's own working
# bucket — so an unconstrained s3_uri is a key-injection vector, not merely a
# cross-account read.
# ---------------------------------------------------------------------------


def test_compressed_ref_in_foreign_bucket_is_rejected(monkeypatch):
    """A hook may not point the next step at another bucket. Downstream only the
    KEY survives, so `s3://attacker/x/evil.json` would be read as
    `s3://<working-bucket>/x/evil.json` — escaping the compressed_documents/
    prefix the design assumes."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("WORKING_BUCKET", "real-wb")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://real-wb/compressed_documents/w2.pdf/1.json",
        "document_id": "w2.pdf",
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://attacker-bucket/compressed_documents/x/evil.json",
                    "document_id": "w2.pdf",
                }
            }
        ),
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert out["document"] == inbound
    assert "not the working bucket" in out["results"][0]["documentUpdateRejected"]


def test_compressed_ref_outside_prefix_is_rejected(monkeypatch):
    """Right bucket, wrong prefix: a hook must not repoint the document at an
    arbitrary object (e.g. another document's sections/N/result.json) that the
    identity check cannot inspect."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("WORKING_BUCKET", "real-wb")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://real-wb/compressed_documents/w2.pdf/1.json",
        "document_id": "w2.pdf",
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://real-wb/other-doc/sections/1/result.json",
                    "document_id": "w2.pdf",
                }
            }
        ),
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert out["document"] == inbound
    assert "outside the" in out["results"][0]["documentUpdateRejected"]


def test_compressed_ref_without_key_is_rejected(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("WORKING_BUCKET", "real-wb")
    mod = _reload()
    inbound = {"id": "w2.pdf"}
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://real-wb",
                    "document_id": "w2.pdf",
                }
            }
        ),
    )
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": inbound}, None)
    assert out["document"] == inbound
    assert "no key" in out["results"][0]["documentUpdateRejected"]


def test_dispatcher_own_inline_wrapper_passes_its_own_validator(monkeypatch):
    """Guards against the two paths drifting: whatever
    _compress_inline_document() writes must satisfy _validate_compressed_ref()."""
    monkeypatch.setenv("WORKING_BUCKET", "real-wb")
    mod = _reload()
    monkeypatch.setattr(mod._s3, "put_object", lambda **kw: {})
    wrapper, reason = mod._compress_inline_document(
        {"id": "w2.pdf", "num_pages": 1, "sections": [{"section_id": "1"}]},
        "postOcr",
        "f",
    )
    assert reason is None and wrapper is not None
    assert mod._validate_compressed_ref(wrapper) is None


def test_foreign_bucket_check_is_skipped_when_working_bucket_unset(monkeypatch):
    """With no WORKING_BUCKET configured the dispatcher cannot know its own
    bucket, so it must not reject every compressed reference outright."""
    monkeypatch.delenv("WORKING_BUCKET", raising=False)
    mod = _reload()
    assert (
        mod._validate_compressed_ref(
            {"compressed": True, "s3_uri": "s3://any-wb/compressed_documents/a/1.json"}
        )
        is None
    )


# ---------------------------------------------------------------------------
# Wrapper fields the STATE MACHINE reads by JSONPath. An absent one is not a
# survivable hook error: an unresolvable ItemsPath/Choice path fails the whole
# execution with States.Runtime (the same class of bug as the dropped use_bda).
# ---------------------------------------------------------------------------


def test_thin_compressed_ref_gets_state_machine_fields_backfilled(monkeypatch):
    """A hand-rolled ref carrying only {compressed, s3_uri, document_id} must
    still reach the workflow with num_pages/status/sections, or
    ProcessSections' ItemsPath ($.ClassificationResult.document.sections) fails
    the execution."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("WORKING_BUCKET", "real-wb")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://real-wb/compressed_documents/w2.pdf/1.json",
        "document_id": "w2.pdf",
        "num_pages": 6,
        "status": "CLASSIFYING",
        "sections": ["1", "2"],
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://real-wb/compressed_documents/w2.pdf/2.json",
                    "document_id": "w2.pdf",
                }
            }
        ),
    )
    out = mod.lambda_handler(
        {"hookPoint": "postClassification", "document": inbound}, None
    )
    doc = out["document"]
    assert doc["s3_uri"].endswith("2.json")  # the hook's change is honored
    assert doc["num_pages"] == 6
    assert doc["status"] == "CLASSIFYING"
    assert doc["sections"] == ["1", "2"]


def test_hook_supplied_wrapper_fields_are_not_overwritten(monkeypatch):
    """Back-filling only fills ABSENT keys — a hook that legitimately changes the
    section list (the whole point of postClassification) must keep its value."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("WORKING_BUCKET", "real-wb")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://real-wb/compressed_documents/w2.pdf/1.json",
        "document_id": "w2.pdf",
        "num_pages": 6,
        "sections": ["1", "2"],
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://real-wb/compressed_documents/w2.pdf/2.json",
                    "document_id": "w2.pdf",
                    "sections": ["1", "2", "3"],
                }
            }
        ),
    )
    out = mod.lambda_handler(
        {"hookPoint": "postClassification", "document": inbound}, None
    )
    assert out["document"]["sections"] == ["1", "2", "3"]  # hook wins
    assert out["document"]["num_pages"] == 6  # absent -> back-filled


# ---------------------------------------------------------------------------
# Active-version resolution (issue #599).
#
# The dispatcher resolves the active config version with a FILTERED scan.
# DynamoDB applies both `Limit` and the implicit 1MB page size to the items it
# EXAMINES, not to the items that pass FilterExpression — so a single scan call
# returns no match whenever the active row sorts beyond the first page, and the
# dispatcher then falls back to Config#default, which carries no feature hooks.
# Every pipeline hook stops firing while the workflow still reports success.
#
# Observed live on a v0.6.3 stack: 35 Config# rows, the active one at scan
# position 33, a 10-item examine window — every hook silently stopped.
# ---------------------------------------------------------------------------


class _PagedScanTable:
    """A table whose scan() reproduces DynamoDB's examine-then-filter paging.

    `rows` is the full scan order. Each call examines at most `page_size` rows,
    applies the IsActive filter to just those, and reports LastEvaluatedKey if
    any rows remain — exactly how the real service bounds a filtered scan.
    Correct callers must page until they find a match or run out of rows.
    """

    def __init__(self, rows, page_size=10):
        self.rows = rows
        self.page_size = page_size
        self.scan_calls = 0

    def scan(self, **kwargs):
        self.scan_calls += 1
        # A Limit, if the caller passes one, narrows the examine window further.
        window = min(self.page_size, kwargs.get("Limit", self.page_size))
        start = 0
        if "ExclusiveStartKey" in kwargs:
            key = kwargs["ExclusiveStartKey"]["Configuration"]
            start = next(
                i + 1 for i, r in enumerate(self.rows) if r["Configuration"] == key
            )
        examined = self.rows[start : start + window]
        matched = [r for r in examined if r.get("IsActive") is True]
        resp = {"Items": [{"Configuration": r["Configuration"]} for r in matched]}
        if start + window < len(self.rows):
            resp["LastEvaluatedKey"] = {"Configuration": examined[-1]["Configuration"]}
        return resp


def _config_rows(active_at, total=35):
    """`total` Config# rows, exactly one of them active, at index `active_at`."""
    return [
        {"Configuration": f"Config#v{i}", "IsActive": i == active_at}
        for i in range(total)
    ]


def test_active_version_found_beyond_the_first_scan_page(monkeypatch):
    """The regression: the active row is at position 33 of 35, well past any
    single examine window. Before the fix this resolved to 'default' and every
    registered hook silently stopped firing."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    table = _PagedScanTable(_config_rows(active_at=33, total=35))

    assert mod._resolve_active_version(table, None) == "v33"
    assert table.scan_calls > 1, "must page rather than trust a single scan call"


def test_active_version_found_on_the_very_last_page(monkeypatch):
    """Boundary: the active row is the final item examined."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    assert (
        mod._resolve_active_version(
            _PagedScanTable(_config_rows(active_at=34, total=35)), None
        )
        == "v34"
    )


def test_active_version_resolution_stops_at_the_first_match(monkeypatch):
    """Paging must not become a full-table walk when the active row is early —
    the dispatcher runs at every hook point of every document."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    table = _PagedScanTable(_config_rows(active_at=0, total=500))
    assert mod._resolve_active_version(table, None) == "v0"
    assert table.scan_calls == 1


def test_no_active_row_falls_back_to_default_with_a_warning(monkeypatch, caplog):
    """A genuine "nothing is active" state still resolves to default, but must
    say so at WARNING: logged at INFO it is indistinguishable from the ordinary
    no-op of a host with no features installed, which is what hid #599."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    rows = [{"Configuration": f"Config#v{i}", "IsActive": False} for i in range(35)]
    with caplog.at_level("WARNING"):
        assert mod._resolve_active_version(_PagedScanTable(rows), None) == "default"
    assert any(
        r.levelname == "WARNING" and "No active Config# version" in r.getMessage()
        for r in caplog.records
    )


def test_scan_failure_falls_back_to_default(monkeypatch):
    """A throttled/failed scan must not fail the workflow."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()

    class _Boom:
        def scan(self, **kwargs):
            raise RuntimeError("throttled")

    assert mod._resolve_active_version(_Boom(), None) == "default"


def test_pinned_version_still_skips_the_scan_entirely(monkeypatch):
    """Pagination must not have introduced a scan on the pinned fast path."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    table = _PagedScanTable(_config_rows(active_at=0))
    assert mod._resolve_active_version(table, "claims-pack-v0.4.0") == (
        "claims-pack-v0.4.0"
    )
    assert table.scan_calls == 0


def test_hooks_resolve_from_a_late_active_row_end_to_end(monkeypatch):
    """Full handler path: with the active row late in scan order, the hook
    registered in THAT version must actually be invoked. This is the assertion
    the live failure would have tripped — `invoked` was 0."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()

    rows = _config_rows(active_at=33, total=35)

    class _Table(_PagedScanTable):
        def get_item(self, Key):
            # Only the active version carries the hook.
            if Key["Configuration"] != "Config#v33":
                return {"Item": {"Configuration": Key["Configuration"]}}
            return {
                "Item": {
                    "Configuration": "Config#v33",
                    "rule_validation": {
                        "postHook": [{"featureId": "claims-pack", "arn": "arn:cp"}]
                    },
                }
            }

    monkeypatch.setattr(mod._dynamodb, "Table", lambda name: _Table(rows))
    monkeypatch.setattr(mod, "_invoke_hook", lambda h, p: _ok({}))

    out = mod.lambda_handler({"hookPoint": "postRuleValidation", "document": {}}, None)
    assert out["invoked"] == 1
    assert out["configVersion"] == "v33"


def test_noop_reports_the_resolved_config_version(monkeypatch):
    """`invoked: 0` alone cannot distinguish "the active version has no hooks
    here" from "we resolved the wrong version", which is why #599 was
    undiagnosable from the state machine output. The no-hooks result now names
    the version it read."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    monkeypatch.setattr(mod, "_resolve_active_version", lambda *a, **k: "v33")
    monkeypatch.setattr(mod, "_read_hooks_from_config", lambda *a, **k: [])
    out = mod.lambda_handler({"hookPoint": "postOcr", "document": {"id": "w2"}}, None)
    assert out["invoked"] == 0
    assert out["configVersion"] == "v33"
    # The state-machine-critical fields are still unconditionally present.
    assert out["halt"] is False
    assert out["document"] == {"id": "w2"}


# ---------------------------------------------------------------------------
# postprocessing — the standalone FINAL hook point (mirror of preprocessing)
# ---------------------------------------------------------------------------


def test_postprocessing_reads_single_flat_hook(monkeypatch):
    """`postprocessing` is a flat single-hook section like `preprocessing`:
    arn/args/onError live directly on it, with no postHook list — even though
    the point name starts with "post"."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()

    class _Table:
        def get_item(self, Key):
            return {
                "Item": {
                    "Configuration": "Config#default",
                    "postprocessing": {
                        "enabled": True,
                        "featureId": "delivery",
                        "arn": "arn:deliver",
                        "onError": "continue",
                        "args": [{"key": "endpoint", "value": "https://sap"}],
                    },
                }
            }

    hooks = mod._read_hooks_from_config(_Table(), "default", "postprocessing")
    assert len(hooks) == 1
    assert hooks[0]["featureId"] == "delivery"
    assert hooks[0]["arn"] == "arn:deliver"
    assert hooks[0]["args"] == [{"key": "endpoint", "value": "https://sap"}]


def test_postprocessing_is_flat_not_list_based(monkeypatch):
    """Regression guard for the prefix heuristic this replaced: `postprocessing`
    must NOT be read as a `postprocessing.postHook` list. A section carrying only
    a postHook list yields no hook (there is no flat arn), which would have
    silently swallowed a misconfigured entry."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    assert mod._is_flat_hook_point("postprocessing") is True
    assert mod._is_flat_hook_point("preprocessing") is True
    assert mod._is_flat_hook_point("postOcr") is False

    class _Table:
        def get_item(self, Key):
            return {
                "Item": {
                    "Configuration": "Config#default",
                    "postprocessing": {
                        "postHook": [{"featureId": "x", "arn": "arn:x"}],
                    },
                }
            }

    assert mod._read_hooks_from_config(_Table(), "default", "postprocessing") == []


def test_postprocessing_disabled_or_arnless_yields_no_hook(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()

    class _Disabled:
        def get_item(self, Key):
            return {
                "Item": {
                    "Configuration": "Config#d",
                    "postprocessing": {"enabled": False, "arn": "arn:x"},
                }
            }

    class _NoArn:
        def get_item(self, Key):
            return {
                "Item": {
                    "Configuration": "Config#d",
                    "postprocessing": {"enabled": True},
                }
            }

    assert mod._read_hooks_from_config(_Disabled(), "d", "postprocessing") == []
    assert mod._read_hooks_from_config(_NoArn(), "d", "postprocessing") == []


def test_postprocessing_hook_can_mutate_the_final_document(monkeypatch):
    """A postprocessing mutation is the last word on the document: the state
    machine copies it into the workflow output, which the workflow tracker
    persists."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "compressed": True,
        "s3_uri": "s3://wb/compressed_documents/w2.pdf/eval.json",
        "document_id": "w2.pdf",
    }
    _mutation_env(
        monkeypatch,
        mod,
        [_hook()],
        lambda h, p: _ok(
            {
                "updatedDocument": {
                    "compressed": True,
                    "s3_uri": "s3://wb/compressed_documents/w2.pdf/delivered.json",
                    "document_id": "w2.pdf",
                }
            }
        ),
    )
    out = mod.lambda_handler({"hookPoint": "postprocessing", "document": inbound}, None)
    assert out["document"]["s3_uri"].endswith("delivered.json")
    assert out["documentUpdatedBy"] == ["f"]


def test_postprocessing_halt_request_is_ignored(monkeypatch):
    """There is nothing downstream to skip at `postprocessing`, so a `halt` is
    reported as ignored rather than returned as a halt the state machine would
    silently drop."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    _mutation_env(monkeypatch, mod, [_hook()], lambda h, p: _ok({"halt": True}))
    out = mod.lambda_handler(
        {"hookPoint": "postprocessing", "document": {"id": "w2"}}, None
    )
    assert out["halt"] is False
    assert out["haltIgnored"] is True
    # The document still flows through untouched.
    assert out["document"] == {"id": "w2"}


def test_preprocessing_halt_still_honored(monkeypatch):
    """Guard the other side of _HALT_CAPABLE_POINTS: gating `halt` must not
    have broken the PII-redaction short-circuit."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    _mutation_env(monkeypatch, mod, [_hook()], lambda h, p: _ok({"halt": True}))
    out = mod.lambda_handler(
        {"hookPoint": "preprocessing", "document": {"id": "w2"}}, None
    )
    assert out["halt"] is True
    assert "haltIgnored" not in out


def test_postprocessing_hook_sees_hitl_status(monkeypatch):
    """The hook decides what to do based on HITL state, so the fields the
    document carries must reach it verbatim (the workflow does not skip the hook
    while a review is pending)."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    mod = _reload()
    inbound = {
        "id": "w2.pdf",
        "hitl_status": "PendingReview",
        "hitl_triggered": True,
        "hitl_sections_pending": ["1"],
    }
    seen = {}

    def _capture(h, payload):
        seen.update(payload)
        return _ok({})

    _mutation_env(monkeypatch, mod, [_hook()], _capture)
    mod.lambda_handler({"hookPoint": "postprocessing", "document": inbound}, None)
    assert seen["document"]["hitl_status"] == "PendingReview"
    assert seen["document"]["hitl_triggered"] is True
    assert seen["hookPoint"] == "postprocessing"
