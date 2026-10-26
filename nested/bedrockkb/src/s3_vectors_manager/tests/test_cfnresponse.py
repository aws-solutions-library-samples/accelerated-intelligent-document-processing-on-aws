# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The vendored ``cfnresponse.py`` in each ``nested/bedrockkb`` Lambda.

The three custom-resource Lambdas under ``nested/bedrockkb/src`` used to install
``cfnresponse`` by bare name from the public package index, unpinned. Each now
carries a vendored copy beside its handler. These tests hold three properties:

- no Lambda there can install it from an index: every directory whose code
  imports ``cfnresponse`` has the vendored copy, and no ``requirements*.txt``
  under ``nested/bedrockkb`` names it;
- the copies are byte-identical, so a fix to one cannot silently miss another;
- ``send`` does what the AWS-published module does (the response body, the
  PUT, the empty Content-Type, a failure logged rather than raised) and adds an
  explicit timeout.

The module is loaded from its file under a private name, because the
directory's ``conftest.py`` puts a stub in ``sys.modules["cfnresponse"]``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import urllib.error
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[2]
BEDROCKKB = SRC.parent
IMPORT = re.compile(r"^\s*(import cfnresponse\b|from cfnresponse import\b)", re.M)


def _importing_dirs() -> list[Path]:
    """Every Lambda source directory under ``nested/bedrockkb/src`` whose own
    code (not its tests) imports ``cfnresponse``."""
    dirs = set()
    for path in SRC.rglob("*.py"):
        if (
            path.name == "cfnresponse.py"
            or "tests" in path.parts
            or path.name.startswith(("test_", "conftest"))
        ):
            continue
        if IMPORT.search(path.read_text()):
            dirs.add(path.parent)
    return sorted(dirs)


IMPORTING_DIRS = _importing_dirs()


def _load(directory: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"vendored_cfnresponse_{directory.name}", directory / "cfnresponse.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVENT = {
    "ResponseURL": "https://cloudformation-custom-resource-response.example/presigned?X-Amz-Signature=abc",
    "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/s/x",
    "RequestId": "req-1",
    "LogicalResourceId": "Resource",
}
CONTEXT = SimpleNamespace(log_stream_name="2026/10/26/[$LATEST]abc")


class _Reply:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture(params=IMPORTING_DIRS, ids=lambda d: d.name)
def vendored(request, monkeypatch):
    module = _load(request.param)
    sent: list[tuple[object, object]] = []

    def urlopen(req, timeout=None):
        sent.append((req, timeout))
        return _Reply()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    return SimpleNamespace(module=module, sent=sent)


def test_the_importers_are_the_three_custom_resource_lambdas():
    """Non-vacuity: the discovery above finds the directories it is about."""
    assert {d.name for d in IMPORTING_DIRS} == {
        "opensearch_index_custom_resource",
        "s3_vectors_manager",
        "start_ingestion_job_custom_resource",
    }


@pytest.mark.parametrize("directory", IMPORTING_DIRS, ids=lambda d: d.name)
def test_every_importer_carries_the_vendored_module(directory):
    assert (directory / "cfnresponse.py").is_file()


def test_no_requirements_file_installs_cfnresponse():
    manifests = sorted(BEDROCKKB.rglob("requirements*.txt"))
    assert manifests, "no requirements files found; the check would be vacuous"
    for manifest in manifests:
        for line in manifest.read_text().splitlines():
            requirement = line.split("#", 1)[0].strip()
            name = re.split(r"[\s\[<>=!~;@]", requirement, maxsplit=1)[0]
            assert name.lower().replace("_", "-") != "cfnresponse", (manifest, line)


def test_the_vendored_copies_are_identical():
    contents = {d.name: (d / "cfnresponse.py").read_bytes() for d in IMPORTING_DIRS}
    assert len(set(contents.values())) == 1, sorted(contents)


def test_send_puts_the_published_response_body(vendored):
    vendored.module.send(
        EVENT,
        CONTEXT,
        vendored.module.SUCCESS,
        {"BucketName": "b"},
        physicalResourceId="s3vectors://b/i",
        noEcho=True,
        reason="done",
    )

    ((req, timeout),) = vendored.sent
    assert req.full_url == EVENT["ResponseURL"]
    assert req.get_method() == "PUT"
    assert json.loads(req.data) == {
        "Status": "SUCCESS",
        "Reason": "done",
        "PhysicalResourceId": "s3vectors://b/i",
        "StackId": EVENT["StackId"],
        "RequestId": "req-1",
        "LogicalResourceId": "Resource",
        "NoEcho": True,
        "Data": {"BucketName": "b"},
    }
    # urllib stores header names capitalized; an empty Content-Type, as the
    # published module sends, keeps urllib from adding a form encoding.
    assert req.get_header("Content-type") == ""
    assert req.get_header("Content-length") == str(len(req.data))
    assert timeout == vendored.module.TIMEOUT_SECONDS
    assert 0 < vendored.module.TIMEOUT_SECONDS <= 20


def test_send_defaults_match_the_published_module(vendored):
    vendored.module.send(EVENT, CONTEXT, vendored.module.FAILED, {})

    ((req, _timeout),) = vendored.sent
    body = json.loads(req.data)
    assert body["Status"] == "FAILED"
    assert body["PhysicalResourceId"] == CONTEXT.log_stream_name
    assert body["Reason"] == (
        f"See the details in CloudWatch Log Stream: {CONTEXT.log_stream_name}"
    )
    assert body["NoEcho"] is False


def test_positional_arguments_keep_the_published_order(vendored):
    """Callers pass physicalResourceId (and one passes a sixth argument)
    positionally, so the order is part of the interface."""
    vendored.module.send(EVENT, CONTEXT, "SUCCESS", {}, "pid", False, "why")

    body = json.loads(vendored.sent[0][0].data)
    assert (body["PhysicalResourceId"], body["NoEcho"], body["Reason"]) == (
        "pid",
        False,
        "why",
    )


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("timed out"),
        TimeoutError("timed out"),
        urllib.error.HTTPError(EVENT["ResponseURL"], 403, "Forbidden", {}, None),  # type: ignore[arg-type]
    ],
    ids=["url-error", "timeout", "http-403"],
)
def test_a_failed_put_is_logged_not_raised(vendored, monkeypatch, caplog, error):
    def urlopen(_req, timeout=None):
        raise error

    monkeypatch.setattr(vendored.module.urllib.request, "urlopen", urlopen)

    vendored.module.send(EVENT, CONTEXT, "SUCCESS", {})

    assert "failed executing the PUT request" in caplog.text


@pytest.mark.parametrize(
    "url", ["http://example.invalid/x", "file:///etc/passwd", "ftp://example.invalid/x"]
)
def test_only_an_https_url_is_opened(vendored, url, caplog):
    vendored.module.send(dict(EVENT, ResponseURL=url), CONTEXT, "SUCCESS", {})

    assert vendored.sent == []
    assert "not an https URL" in caplog.text, "a refused send must be visible"


def test_the_presigned_url_is_not_logged(vendored, caplog):
    caplog.set_level("DEBUG")
    vendored.module.send(EVENT, CONTEXT, "SUCCESS", {})

    assert "X-Amz-Signature" not in caplog.text
