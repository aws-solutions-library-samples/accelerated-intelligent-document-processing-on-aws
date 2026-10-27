# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A handler's invocation deadline ends with the invocation that published it.

Three deployed entry points publish the time their invocation must finish, so the
Bedrock retry ladders they reach stop before the function is killed: both extraction
handlers and the assessment handler. ``set_lambda_deadline_epoch`` writes process-wide
state as well as a ContextVar, and each of them used to leave both set when it
returned, so whatever ran next in the same process read a deadline belonging to an
invocation that had already ended. In a test run that is every later test on the same
worker: once the 300 s the handler tests publish had passed, the retry ladders in
``tests/unit/utils/test_image_rejection_not_retried.py`` gave up after their first
failure, and four of its tests failed only in a whole-suite run.

Each handler is driven here through ``handler``, the entry point Lambda calls, with
its service stubbed to record the deadline in force while it runs. That record is what
keeps the second half honest: a handler that stopped publishing would leave nothing
behind too, and would pass a check of what is left over alone.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import botocore.exceptions
import pytest

from idp_common.config.models import IDPConfig
from idp_common.lambda_deadline import (
    get_lambda_deadline_epoch,
    set_lambda_deadline_epoch,
)
from idp_common.models import Document, Section, Status
from idp_common.utils.transient_errors import TransientError

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[5]
HANDLER_SOURCE_ROOT = REPO_ROOT / "patterns" / "unified" / "src"
REMAINING_SECONDS = 300.0


def _load(module_name: str, relative_path: str) -> ModuleType:
    """Load a deployed handler by path, with X-Ray's import-time work stubbed.

    Only the two X-Ray entries are swapped into ``sys.modules`` and put back. A
    ``patch.dict`` over all of ``sys.modules`` would also evict every module the
    handler imported for the first time, so a later test in the same process would
    patch a second copy of ``idp_common.s3`` while the code under test still called
    the first.
    """
    recorder = MagicMock()
    recorder.capture.return_value = lambda fn: fn
    xray_core = MagicMock()
    xray_core.patch_all = lambda: None
    xray_core.xray_recorder = recorder
    stubs = {"aws_xray_sdk": MagicMock(), "aws_xray_sdk.core": xray_core}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, HANDLER_SOURCE_ROOT / relative_path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module


extraction_index = _load(
    "deadline_scope_extraction_index", "extraction_function/index.py"
)
shard_runtime = _load(
    "deadline_scope_sfn_runtime_handler", "extraction_function/sfn_runtime_handler.py"
)
assessment_index = _load(
    "deadline_scope_assessment_index", "assessment_function/index.py"
)


class _Context:
    function_name = "deadline-scope"
    memory_limit_in_mb = 1024
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self) -> int:
        return int(REMAINING_SECONDS * 1000)


def _document() -> Document:
    return Document(
        id="doc.pdf",
        input_bucket="in",
        input_key="doc.pdf",
        output_bucket="out",
        status=Status.CLASSIFYING,
        num_pages=1,
        pages={},
        sections=[Section(section_id="1", classification="w2", page_ids=["1"])],
    )


def _read_timeout() -> botocore.exceptions.ReadTimeoutError:
    return botocore.exceptions.ReadTimeoutError(
        endpoint_url="https://bedrock-runtime.example.invalid/"
    )


def _read_from_a_worker() -> float | None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(get_lambda_deadline_epoch).result()


def _service_call(seen: list[float | None], fail: bool, value: Callable[..., Any]):
    def _call(*args: Any, **kwargs: Any) -> Any:
        seen.append(get_lambda_deadline_epoch())
        if fail:
            raise _read_timeout()
        return value(*args, **kwargs)

    return _call


def _drive_extraction(monkeypatch, seen, fail):
    monkeypatch.setenv("WORKING_BUCKET", "")
    monkeypatch.setattr(extraction_index, "get_config", lambda **kw: IDPConfig())
    monkeypatch.setattr(
        extraction_index, "create_document_service", lambda *a, **kw: MagicMock()
    )
    monkeypatch.setattr(extraction_index, "metrics", MagicMock())
    service = MagicMock()
    service.process_document_section.side_effect = _service_call(
        seen, fail, lambda **kw: kw["document"]
    )
    monkeypatch.setattr(
        extraction_index,
        "extraction",
        SimpleNamespace(ExtractionService=lambda **kw: service),
    )
    return extraction_index.handler(
        {"document": _document().to_dict(), "section_id": "1"}, _Context()
    )


def _drive_shard_runtime(monkeypatch, seen, fail):
    monkeypatch.setattr(
        shard_runtime, "_load", lambda event: ("", _document(), IDPConfig())
    )
    service = MagicMock()
    service.plan_section_shards.side_effect = _service_call(
        seen, fail, lambda **kw: {"shard_mode": "single", "num_shards": 1}
    )
    monkeypatch.setattr(
        shard_runtime,
        "extraction",
        SimpleNamespace(ExtractionService=lambda **kw: service),
    )
    return shard_runtime.handler(
        {"mode": "plan", "section_id": "1", "execution_arn": "arn:exec"}, _Context()
    )


def _drive_assessment(monkeypatch, seen, fail):
    monkeypatch.setenv("WORKING_BUCKET", "")
    monkeypatch.setattr(assessment_index, "get_config", lambda **kw: MagicMock())
    monkeypatch.setattr(
        assessment_index, "create_document_service", lambda *a, **kw: MagicMock()
    )
    service = MagicMock()
    service.process_document_section.side_effect = _service_call(
        seen, fail, lambda document, *a, **kw: document
    )
    monkeypatch.setattr(
        assessment_index,
        "assessment",
        SimpleNamespace(AssessmentService=lambda **kw: service),
    )
    return assessment_index.handler(
        {"document": _document().to_dict(), "section_id": "1"}, _Context()
    )


DRIVEN_PUBLISHERS: dict[str, Callable[..., Any]] = {
    "patterns/unified/src/extraction_function/index.py": _drive_extraction,
    "patterns/unified/src/extraction_function/sfn_runtime_handler.py": (
        _drive_shard_runtime
    ),
    "patterns/unified/src/assessment_function/index.py": _drive_assessment,
}


@pytest.fixture(autouse=True)
def _no_deadline_in_force():
    set_lambda_deadline_epoch(None)
    yield
    set_lambda_deadline_epoch(None)


def _invoke(drive, monkeypatch, fail: bool) -> tuple[float | None, float, float]:
    seen: list[float | None] = []
    started = time.time()
    if fail:
        with pytest.raises(TransientError):
            drive(monkeypatch, seen, fail)
    else:
        drive(monkeypatch, seen, fail)
    finished = time.time()
    assert len(seen) == 1, "the stubbed service call was not reached"
    return seen[0], started, finished


@pytest.mark.parametrize("fail", [False, True], ids=["returns", "raises"])
@pytest.mark.parametrize("publisher", sorted(DRIVEN_PUBLISHERS))
def test_the_deadline_a_handler_publishes_ends_with_its_invocation(
    publisher, fail, monkeypatch
):
    """Published while the handler runs, gone once it has returned or raised.

    Read from this thread and from a pool worker after the call, because either store
    left behind is the leak: the ContextVar is what the next caller on this thread
    reads first, and the process-wide value is what every thread falls back to.
    """
    published, started, finished = _invoke(
        DRIVEN_PUBLISHERS[publisher], monkeypatch, fail
    )

    assert published is not None, f"{publisher} no longer publishes its deadline"
    assert started + REMAINING_SECONDS <= published <= finished + REMAINING_SECONDS

    assert get_lambda_deadline_epoch() is None, (
        f"{publisher} left its invocation's deadline in force after "
        f"{'raising' if fail else 'returning'}; everything that runs next in this "
        "process inherits it, and once it has passed every retry ladder there gives "
        "up after its first failure"
    )
    assert _read_from_a_worker() is None, (
        f"{publisher} left its deadline in the process-wide store a thread-pool "
        "worker reads"
    )


@pytest.mark.parametrize("publisher", sorted(DRIVEN_PUBLISHERS))
def test_a_deadline_already_in_force_is_back_after_the_handler(publisher, monkeypatch):
    """Restored rather than cleared: a caller holding a deadline of its own gets it
    back, in both stores, and the handler still publishes its own while it runs."""
    outer = time.time() + 10 * REMAINING_SECONDS
    set_lambda_deadline_epoch(outer)

    published, started, finished = _invoke(
        DRIVEN_PUBLISHERS[publisher], monkeypatch, fail=False
    )

    assert published is not None
    assert started + REMAINING_SECONDS <= published <= finished + REMAINING_SECONDS
    assert get_lambda_deadline_epoch() == outer
    assert _read_from_a_worker() == outer


def _calls_the_publisher(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "set_lambda_deadline_epoch":
            return True
    return False


def _is_test_code(relative: str) -> bool:
    path = Path(relative)
    return (
        path.name.startswith("test_")
        or path.name == "conftest.py"
        or bool({"tests", "manual_tests"} & set(path.parts))
    )


def test_every_module_that_publishes_the_deadline_is_driven_here():
    """Universe closure over :data:`DRIVEN_PUBLISHERS`, derived from the tree.

    The assertions above hold for the handlers they are given. A new publisher that is
    not driven here could leave its deadline behind with every gate green, and the
    first sign would be a retry test failing minutes later in an unrelated module in a
    whole-suite run, which is what made this leak expensive to find. So the set is
    read out of every Python file git does not ignore, outside the test trees, as real
    calls rather than as text, and must equal the set driven here.
    """
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.py",
        ],
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout.decode("utf-8")
    publishers = set()
    for relative in filter(None, listed.split("\0")):
        path = REPO_ROOT / relative
        if _is_test_code(relative) or not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "set_lambda_deadline_epoch" in source and _calls_the_publisher(source):
            publishers.add(relative)

    assert publishers, "found no publisher at all: the derivation is broken"
    assert publishers == set(DRIVEN_PUBLISHERS), (
        f"publishers not driven here: {sorted(publishers - set(DRIVEN_PUBLISHERS))}; "
        f"driven here but no longer publishing: "
        f"{sorted(set(DRIVEN_PUBLISHERS) - publishers)}. A module that calls "
        "set_lambda_deadline_epoch must run its whole invocation inside "
        "lambda_deadline_scope and be driven by a test in this file."
    )
