# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every transient-conversion site in rule validation, and a test that dies with it.

#1101 put a transient-error classification at thirteen ``except``/gather sites
across the rule-validation feature. A mutation sweep for #1141 — neutralising one
site at a time and re-running the three suites that name them — found that eight
of the thirteen could be removed with every test still green. Removing a
conversion means the fault it converts goes back to being answered with a result:
a fabricated "Information Not Found" verdict, an empty consolidation the caller
reads as "there was nothing to consolidate", or a section evaluated against a
prompt with no extracted data. Those are the sites where a later edit could
restore absorb-and-continue with nothing to notice.

``test_transient_retry.py`` next door is the behavioural suite: it asserts the
user-visible failure forms and the nesting composition, in the detail each
deserves. This file is the **matrix**, and it exists to close the universe:

* The thirteen sites are **derived from the source**, not listed here. A
  hardcoded inventory is the defect class this file is about — a universe with
  nothing closing over it — so ``discover_sites()`` parses the feature's modules
  with ``ast`` and ``test_every_discovered_site_has_an_entry`` fails in both
  directions: a new site with no entry, and an entry naming a site that is gone.
  Sites are keyed by ``<path>::<enclosing function>#<n>``, not by line, because a
  line number drifts on the first edit above it while the key does not.
* Each site is driven through the **real object**, never the helper. One test in
  #1131 asserted ``reraise_if_transient`` inline, passed, and was green against
  removal of the call site it was named for.
* Each site gets the two-case pair — a transient cause surfaces as
  ``TransientError`` with ``__cause__`` intact, and a deterministic cause keeps
  today's fallback — plus a third case that is the point of the file:
  ``test_removing_the_conversion_at_this_site_reddens_its_own_test`` re-runs the
  transient case with that one site neutralised and fails if it still passes.
  That is mutation coverage for these thirteen sites, run in-process, and it is
  what makes "somebody remembered" into "the tree checks".

The neutralisation is per **line**, not per module: the patched helper reads its
caller's frame and no-ops only for the one call site under test, delegating to the
real helper everywhere else. Five of the thirteen sites are in one module and two
are in one function, so a module-wide patch would answer about the module and not
about the site — and for the nested handlers it would answer about an outer site
that is not the one named.

Out of scope, stated so a green result is not over-read: the same helpers are
called from the extraction and assessment handlers, which are a different
feature with their own suites. ``discover_sites()`` selects by path, so those are
not in this matrix; a new rule-validation module, however, is.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import botocore.exceptions
import pytest

from idp_common.config.models import IDPConfig
from idp_common.models import Document, Page, Section, Status
from idp_common.rule_validation import orchestrator as orchestrator_module
from idp_common.rule_validation import policy_classification as pc_module
from idp_common.rule_validation import service as service_module
from idp_common.rule_validation.orchestrator import RuleValidationOrchestratorService
from idp_common.rule_validation.policy_classification import PolicyClassificationService
from idp_common.rule_validation.service import RuleValidationService
from idp_common.utils.transient_errors import TransientError

REPO_ROOT = Path(__file__).resolve().parents[5]
PATTERN_SRC = REPO_ROOT / "patterns" / "unified" / "src"

#: The two helpers #1101 introduced. ``reraise_if_transient`` is for an ``except``
#: that returns; ``raise_if_transient`` for one ending in a bare ``raise``.
HELPERS = ("raise_if_transient", "reraise_if_transient")


# ---------------------------------------------------------------------------
# Site discovery: parse the feature's sources, do not list them
# ---------------------------------------------------------------------------


def _is_rule_validation_path(rel: str) -> bool:
    """The feature's own files, under either spelling of its name.

    ``idp_common/rule_validation/`` for the library and
    ``rule-validation-*-function/`` for the three Lambdas. Selecting by path is
    what puts a *new* module in this matrix automatically, and what keeps the
    extraction and assessment call sites out of it.
    """
    return "rule_validation/" in rel or "rule-validation" in rel


def _python_sources() -> list[Path]:
    roots = [
        REPO_ROOT / "lib" / "idp_common_pkg" / "idp_common",
        PATTERN_SRC,
    ]
    found: list[Path] = []
    for root in roots:
        found.extend(sorted(root.rglob("*.py")))
    return [p for p in found if "__pycache__" not in p.parts]


def discover_sites() -> list[tuple[str, int, str]]:
    """``(site_id, line, helper)`` for every call in the rule-validation feature.

    ``site_id`` is ``<repo-relative path>::<enclosing function>#<n>``, with ``n``
    counting calls within that function in line order.
    """
    sites: list[tuple[str, int, str]] = []
    for path in _python_sources():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not _is_rule_validation_path(rel):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        calls: list[tuple[int, str, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else (func.attr if isinstance(func, ast.Attribute) else None)
            )
            if name not in HELPERS:
                continue
            enclosing = "<module>"
            walker: ast.AST | None = parents.get(node)
            while walker is not None:
                if isinstance(walker, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    enclosing = walker.name
                    break
                walker = parents.get(walker)
            calls.append((node.lineno, enclosing, name))

        seen: dict[str, int] = {}
        for line, enclosing, name in sorted(calls):
            seen[enclosing] = seen.get(enclosing, 0) + 1
            sites.append((f"{rel}::{enclosing}#{seen[enclosing]}", line, name))
    return sorted(sites)


# ---------------------------------------------------------------------------
# Per-line neutralisation: the mutation, applied in-process
# ---------------------------------------------------------------------------


def _load_handler(module_name: str, relative_path: str) -> ModuleType:
    """Load a deployed handler by path, with X-Ray's import-time work stubbed.

    By path because ``patterns/unified/src`` is not on the test path and two of
    these directories are not importable names (hyphens). Same mechanism as
    ``tests/unit/lambdas/test_document_failure_persistence.py``, minus that
    harness's ``os.environ.setdefault("AWS_REGION", ...)``: naming these three
    modules as slash-joined literals puts them into
    ``patterns/unified/tests/test_handler_imports_are_region_free.py``'s derived
    universe, and one of them read ``os.environ['AWS_REGION']`` at import. That
    read moved to the invocation in the same change, so no region is needed here
    — which is the point of the gate, since a ``setdefault`` in a test harness
    undoes the environment stripping for everything the harness imports.
    """
    recorder = MagicMock()
    recorder.capture.return_value = lambda fn: fn
    xray_core = MagicMock()
    xray_core.patch_all = lambda: None
    xray_core.xray_recorder = recorder
    with patch.dict(
        "sys.modules",
        {"aws_xray_sdk": MagicMock(), "aws_xray_sdk.core": xray_core},
    ):
        spec = importlib.util.spec_from_file_location(
            module_name, str(PATTERN_SRC / relative_path)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


section_handler = _load_handler("rv_section_index", "rule-validation-function/index.py")
orchestration_handler = _load_handler(
    "rv_orchestration_index", "rule-validation-orchestration-function/index.py"
)
policy_handler = _load_handler(
    "rv_policy_index", "rule-validation-policy-classification-function/index.py"
)

#: Where each source file's helper name is bound at runtime. The neutralisation
#: patches the *importing* module's name, so this maps one to the other.
_MODULE_FOR_PATH: dict[str, ModuleType] = {
    "lib/idp_common_pkg/idp_common/rule_validation/orchestrator.py": (
        orchestrator_module
    ),
    "lib/idp_common_pkg/idp_common/rule_validation/service.py": service_module,
    "lib/idp_common_pkg/idp_common/rule_validation/policy_classification.py": pc_module,
    "patterns/unified/src/rule-validation-function/index.py": section_handler,
    "patterns/unified/src/rule-validation-orchestration-function/index.py": (
        orchestration_handler
    ),
    "patterns/unified/src/rule-validation-policy-classification-function/index.py": (
        policy_handler
    ),
}


@contextmanager
def neutralised(site_id: str, line: int, helper: str):
    """Make the conversion at exactly one call site a no-op.

    The replacement reads its caller's frame and delegates to the real helper for
    every site but the one named, so a module holding five sites still behaves
    normally at the other four. ``f_lineno`` during the call is the first line of
    the call expression, which is what ``ast`` reports, so the two agree for the
    multi-line calls as well. Only the named module's binding is replaced, so a
    call from anywhere else reaches the real helper regardless; the file-name
    check alongside the line is there so the predicate reads as the whole
    condition rather than relying on that.
    """
    rel = site_id.split("::", 1)[0]
    module = _MODULE_FOR_PATH[rel]
    real = getattr(module, helper)
    target_file = Path(rel).name

    def maybe(exc: BaseException, where: str = "") -> None:
        frame = sys._getframe(1)
        if (
            frame.f_lineno == line
            and Path(frame.f_code.co_filename).name == target_file
        ):
            return  # the mutation: this one site does nothing
        real(exc, where)

    with patch.object(module, helper, maybe):
        yield


# ---------------------------------------------------------------------------
# Causes. Provenance is per member; see test_transient_retry.py for the same set.
# ---------------------------------------------------------------------------

#: OBSERVED from a live bedrock-runtime Converse with a 1 ms read timeout.
TRANSIENT_CAUSE = botocore.exceptions.ReadTimeoutError(endpoint_url="https://bedrock")

#: A throttle arriving under a class name no ``Retry`` list carries. CONSTRUCTED:
#: botocore raises a bare ``ClientError`` only when the wire code is absent from
#: the service's error map, which is not the case for bedrock-runtime.
TRANSIENT_CLIENT_ERROR = botocore.exceptions.ClientError(
    {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse"
)

DETERMINISTIC_CAUSE = ValueError("rule schema is malformed")


# ---------------------------------------------------------------------------
# Fixtures the drivers share
# ---------------------------------------------------------------------------

_SERVICE_CONFIG: dict[str, Any] = {
    "rule_validation": {
        "fact_extraction": {
            "model": "us.amazon.nova-lite-v1:0",
            "system_prompt": "s",
            "task_prompt": "{DOCUMENT_TEXT} {rule}",
            "temperature": 0.0,
            "top_k": 1,
            "top_p": 0.1,
            "max_tokens": 100,
        }
    }
}

_ORCHESTRATOR_CONFIG: dict[str, Any] = {
    "rule_validation": {
        "rule_validation_orchestrator": {
            "model": "us.amazon.nova-lite-v1:0",
            "system_prompt": "s",
            "task_prompt": "{responses}",
            "temperature": 0.0,
            "top_k": 1,
            "top_p": 0.1,
            "max_tokens": 100,
        }
    }
}


def _document(*, extraction_uri: str | None = None) -> Document:
    """One section over one page, with a parsed-text URI so the read is reached."""
    section = Section(section_id="1", classification="w2", page_ids=["1"])
    if extraction_uri:
        section.extraction_result_uri = extraction_uri
    return Document(
        id="internal-doc-id",
        input_bucket="in",
        input_key="doc.pdf",
        output_bucket="out",
        status=Status.RULE_VALIDATION,
        num_pages=1,
        pages={"1": Page(page_id="1", parsed_text_uri="s3://out/doc.pdf/1/parsed.txt")},
        sections=[section],
    )


def _service() -> RuleValidationService:
    return RuleValidationService(region="us-west-2", config=_SERVICE_CONFIG)


def _lambda_context() -> Any:
    class _Context:
        function_name = "fn"
        memory_limit_in_mb = 2048
        invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:fn"
        aws_request_id = "req-1"

        def get_remaining_time_in_millis(self):
            return 300_000

    return _Context()


# ---------------------------------------------------------------------------
# One driver per site. Each drives the REAL object and returns whatever the
# production path returns, so the tests can tell "absorbed" from "re-raised".
# ---------------------------------------------------------------------------

_SUMMARISE_RESPONSES = {"eligibility": [{"rule": "must be employed", "answer": "yes"}]}


def _drive_summarisation_gather(error: BaseException) -> Any:
    """The per-rule summarisation gather: the coroutine fails when awaited.

    Both sites in ``_summarize_responses`` lie on this path — the gather's
    ``return_exceptions=True`` result reaches the first, and the
    ``TransientError`` that raises there travels up into the method's own outer
    handler, which reaches the second. Neutralising either one on its own stops a
    ``TransientError`` escaping, for two different reasons: without the first the
    rule is dropped from the summary with no verdict and no error, and without
    the second the outer handler swallows the first's conversion and returns the
    raw per-section dicts. That pairing is the nesting composition #1101's own
    suite covers, and it is why a module-wide neutralisation would answer about
    neither site.
    """
    orchestrator = RuleValidationOrchestratorService(config=_ORCHESTRATOR_CONFIG)
    with patch.object(
        RuleValidationOrchestratorService,
        "_summarize_single_rule",
        new_callable=AsyncMock,
        side_effect=error,
    ):
        return asyncio.run(
            orchestrator._summarize_responses(
                dict(_SUMMARISE_RESPONSES), _ORCHESTRATOR_CONFIG
            )
        )


def _drive_summarisation_outer(error: BaseException) -> Any:
    """The outer handler reached by a failure that never passed the gather site.

    Driving it needs the failure to arise inside the ``try`` but outside the
    gather's results, so ``asyncio.gather`` itself is made to raise. Patching
    ``_summarize_single_rule`` cannot do it: the method is ``async def``, so
    ``patch.object`` supplies an ``AsyncMock`` whatever is asked for, the
    exception surfaces when the coroutine is awaited, and the path becomes the
    one above. Measured, not assumed — with the helper instrumented, a
    ``side_effect`` on that method reports both sites and this driver reports
    only the outer one.

    ``_summarize_single_rule`` is replaced by a plain synchronous mock here so no
    coroutine is created and abandoned when the gather raises.
    """
    orchestrator = RuleValidationOrchestratorService(config=_ORCHESTRATOR_CONFIG)
    with (
        patch.object(
            RuleValidationOrchestratorService,
            "_summarize_single_rule",
            new=MagicMock(return_value=None),
        ),
        patch("asyncio.gather", side_effect=error),
    ):
        return asyncio.run(
            orchestrator._summarize_responses(
                dict(_SUMMARISE_RESPONSES), _ORCHESTRATOR_CONFIG
            )
        )


def _drive_load_section_results(error: BaseException) -> Any:
    orchestrator = RuleValidationOrchestratorService(config={})
    with patch.object(orchestrator_module.s3, "find_matching_files", side_effect=error):
        return orchestrator.load_section_results("doc.pdf", "out")


def _drive_single_z3_rule(error: BaseException) -> Any:
    orchestrator = RuleValidationOrchestratorService(config={})
    with patch.object(
        RuleValidationOrchestratorService,
        "_collect_facts_across_sections",
        side_effect=error,
    ):
        return asyncio.run(
            orchestrator._process_single_z3_rule(
                compound_key=("eligibility", "r1"),
                rule_id="r1",
                policy_type="eligibility",
                rule_description="must be employed",
                section_responses=[{"facts": []}],
                config={},
            )
        )


def _drive_consolidation(error: BaseException) -> Any:
    orchestrator = RuleValidationOrchestratorService(config={})
    with patch.object(
        RuleValidationOrchestratorService, "load_section_results", side_effect=error
    ):
        return asyncio.run(orchestrator.consolidate_and_save_all(_document(), {}))


def _drive_rule_question(error: BaseException) -> Any:
    with patch.object(RuleValidationService, "_invoke_model_async", side_effect=error):
        return asyncio.run(
            _service()._process_rule_question(
                rule="must be employed",
                user_history="text",
                policy_type="eligibility",
                config=_SERVICE_CONFIG,
            )
        )


def _drive_extraction_results_load(error: BaseException) -> Any:
    """The per-section extraction-results read, inside ``process_one_section``.

    ``process_one_section`` is nested inside ``validate_document_async``, so it
    can only be driven through the public method. The page-text read is made to
    succeed and the model invocation is stubbed, leaving the extraction load as
    the only failing call — otherwise the failure would arrive at the
    document-level handler instead and this driver would answer about that site.
    """
    document = _document(extraction_uri="s3://out/doc.pdf/1/result.json")
    with (
        patch.object(service_module.s3, "get_text_content", return_value="page text"),
        patch.object(service_module.s3, "get_json_content", side_effect=error),
        patch.object(
            RuleValidationService,
            "_invoke_model_async",
            new_callable=AsyncMock,
            return_value={"response": "{}", "metering": {}},
        ),
    ):
        return asyncio.run(
            _service().validate_document_async(document, _SERVICE_CONFIG)
        )


def _drive_document_level(error: BaseException) -> Any:
    """The document-level terminal failure: the unwrapped page-text read."""
    with patch.object(service_module.s3, "get_text_content", side_effect=error):
        return asyncio.run(
            _service().validate_document_async(_document(), _SERVICE_CONFIG)
        )


def _page_content_classifier() -> PolicyClassificationService:
    """Two page-content policy classes and no document-name regex, so the page
    read is the only evidence and skipping it changes the answer."""
    cfg = IDPConfig()
    cfg.policy_classes = [
        {
            "x-aws-idp-policy-type": "medicare",
            "x-aws-idp-document-page-content-regex": r"(?i)medicare number",
        },
        {
            "x-aws-idp-policy-type": "invoice",
            "x-aws-idp-document-page-content-regex": r"(?i)invoice number",
        },
    ]
    return PolicyClassificationService(config=cfg)


def _drive_page_text_read(error: BaseException) -> Any:
    document = Document(id="unknown.pdf")
    document.pages["1"] = Page(page_id="1", parsed_text_uri="s3://bucket/1.txt")
    with patch.object(pc_module.s3, "get_text_content", side_effect=error):
        return _page_content_classifier().classify_document(document)


def _drive_section_handler(error: BaseException) -> Any:
    with patch.object(section_handler, "_handle", side_effect=error):
        return section_handler.handler({"section_id": "1"}, _lambda_context())


def _drive_orchestration_handler(error: BaseException) -> Any:
    """This handler has no ``_handle`` split: its whole body is inside the ``try``.

    The document load is the first call in it, and failing there leaves
    ``document`` unbound, so the ``if "document" in locals()`` recorder in the
    handler's own ``except`` is skipped and nothing reaches DynamoDB.
    """
    with patch.object(
        orchestration_handler.Document, "load_document", side_effect=error
    ):
        return orchestration_handler.handler({}, _lambda_context())


def _drive_policy_handler(error: BaseException) -> Any:
    with patch.object(policy_handler, "_handle", side_effect=error):
        return policy_handler.handler({}, _lambda_context())


def _drive_stale_result_cleanup(error: BaseException) -> Any:
    """The stale-result cleanup, which deletes the previous run's section results.

    The orchestrator globs exactly the prefix this removes, so a cleanup that did
    not happen means last run's verdicts are consolidated as this run's.
    """
    client = MagicMock()
    client.list_objects_v2.side_effect = error
    with patch("boto3.client", return_value=client):
        return policy_handler._cleanup_rule_validation_files("out", "doc.pdf")


# ---------------------------------------------------------------------------
# The registry. Membership is derived (above); only the driver and the expected
# deterministic outcome are authored here.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteCase:
    #: What the production path does with a DETERMINISTIC cause today. Pinned so
    #: the split cannot quietly become "raise everything", which would retry an
    #: unparseable document through the whole ladder.
    on_deterministic: str  # "absorbs" | "reraises"
    drive: Callable[[BaseException], Any]
    #: Causes to drive with. Both helpers walk ``__cause__``, so one observed and
    #: one constructed transient shape is enough per site.
    transient: tuple[BaseException, ...] = field(
        default=(TRANSIENT_CAUSE, TRANSIENT_CLIENT_ERROR)
    )


SITES: dict[str, SiteCase] = {
    "lib/idp_common_pkg/idp_common/rule_validation/orchestrator.py::"
    "_summarize_responses#1": SiteCase(
        on_deterministic="absorbs", drive=_drive_summarisation_gather
    ),
    "lib/idp_common_pkg/idp_common/rule_validation/orchestrator.py::"
    "_summarize_responses#2": SiteCase(
        on_deterministic="absorbs", drive=_drive_summarisation_outer
    ),
    "lib/idp_common_pkg/idp_common/rule_validation/orchestrator.py::"
    "load_section_results#1": SiteCase(
        on_deterministic="absorbs", drive=_drive_load_section_results
    ),
    "lib/idp_common_pkg/idp_common/rule_validation/orchestrator.py::"
    "_process_single_z3_rule#1": SiteCase(
        on_deterministic="absorbs", drive=_drive_single_z3_rule
    ),
    "lib/idp_common_pkg/idp_common/rule_validation/orchestrator.py::"
    "consolidate_and_save_all#1": SiteCase(
        on_deterministic="absorbs", drive=_drive_consolidation
    ),
    "lib/idp_common_pkg/idp_common/rule_validation/service.py::"
    "_process_rule_question#1": SiteCase(
        on_deterministic="absorbs", drive=_drive_rule_question
    ),
    "lib/idp_common_pkg/idp_common/rule_validation/service.py::"
    "process_one_section#1": SiteCase(
        on_deterministic="absorbs", drive=_drive_extraction_results_load
    ),
    "lib/idp_common_pkg/idp_common/rule_validation/service.py::"
    "validate_document_async#1": SiteCase(
        on_deterministic="absorbs", drive=_drive_document_level
    ),
    "lib/idp_common_pkg/idp_common/rule_validation/policy_classification.py::"
    "_run_regex_checks#1": SiteCase(
        on_deterministic="absorbs", drive=_drive_page_text_read
    ),
    "patterns/unified/src/rule-validation-function/index.py::handler#1": SiteCase(
        on_deterministic="reraises", drive=_drive_section_handler
    ),
    "patterns/unified/src/rule-validation-orchestration-function/index.py::"
    "handler#1": SiteCase(
        on_deterministic="reraises", drive=_drive_orchestration_handler
    ),
    "patterns/unified/src/rule-validation-policy-classification-function/index.py::"
    "handler#1": SiteCase(on_deterministic="reraises", drive=_drive_policy_handler),
    "patterns/unified/src/rule-validation-policy-classification-function/index.py::"
    "_cleanup_rule_validation_files#1": SiteCase(
        on_deterministic="absorbs", drive=_drive_stale_result_cleanup
    ),
}

_DISCOVERED = discover_sites()
_LINES = {site_id: line for site_id, line, _ in _DISCOVERED}
_HELPERS = {site_id: helper for site_id, _, helper in _DISCOVERED}

#: Parametrisation is over the REGISTRY, not over the registry intersected with
#: what discovery found. The intersection was the first spelling and it is wrong
#: in the way this file is about: deleting a conversion drops the site out of
#: ``_LINES``, so its behavioural test would not have failed — it would have
#: stopped existing, leaving only the closure test to report a removal that could
#: have been anywhere. Driving from the registry makes the site's own named test
#: go red. A site present here and absent from the source fails
#: ``test_every_discovered_site_has_an_entry`` as well, which is the intended pair
#: of signals rather than a duplicate.
_DRIVEN = sorted(SITES)


# ---------------------------------------------------------------------------
# Closure over the universe
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_discovery_finds_the_features_call_sites():
    """Fail loudly rather than pass vacuously if discovery returns nothing."""
    assert len(_DISCOVERED) >= 13, (
        f"only {len(_DISCOVERED)} transient-conversion sites discovered in the "
        "rule-validation feature; the matrix below would prove nothing"
    )


@pytest.mark.unit
def test_every_discovered_site_has_an_entry():
    """Both directions. An unregistered site is an untested one; a registered
    site that no longer exists is a driver pointed at nothing."""
    discovered = set(_LINES)
    registered = set(SITES)
    assert not (discovered - registered), (
        "transient-conversion sites in rule validation with no entry in SITES — "
        "add a driver that dies when the conversion is removed:\n"
        + "\n".join(
            f"  {s} (line {_LINES[s]})" for s in sorted(discovered - registered)
        )
    )
    assert not (registered - discovered), (
        "SITES entries naming a call site that is no longer in the source:\n"
        + "\n".join(f"  {s}" for s in sorted(registered - discovered))
    )


@pytest.mark.unit
def test_both_helpers_are_represented():
    """The two helpers are not interchangeable — one returns silently for an
    already-surfaced ``TransientError`` and one re-raises it — so a matrix that
    happened to cover only one spelling would say nothing about the other."""
    registered = set(_HELPERS) & set(SITES)
    assert set(_HELPERS[s] for s in registered) == set(HELPERS)


# ---------------------------------------------------------------------------
# The two-case pair, per site
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("site_id", _DRIVEN)
def test_a_transient_cause_surfaces_under_the_retried_name(site_id):
    case = SITES[site_id]
    for cause in case.transient:
        with pytest.raises(TransientError) as surfaced:
            case.drive(cause)
        assert surfaced.value.__cause__ is cause, (
            f"{site_id} surfaced a TransientError but lost the cause, so the "
            "original failure is no longer readable in the logs"
        )


@pytest.mark.unit
@pytest.mark.parametrize("site_id", _DRIVEN)
def test_a_deterministic_cause_keeps_todays_behaviour(site_id):
    """Eight more attempts cannot validate a document that fails the same way
    every time, so the deterministic half of each split is pinned as well."""
    case = SITES[site_id]
    try:
        case.drive(DETERMINISTIC_CAUSE)
    except TransientError as surfaced:  # pragma: no cover - the failure path
        pytest.fail(
            f"{site_id} escalated a deterministic failure to TransientError: {surfaced}"
        )
    except Exception as raised:
        assert case.on_deterministic == "reraises", (
            f"{site_id} is registered as absorbing a deterministic failure but "
            f"raised {type(raised).__name__}"
        )
        return
    assert case.on_deterministic == "absorbs", (
        f"{site_id} is registered as re-raising a deterministic failure but "
        "returned normally"
    )


# ---------------------------------------------------------------------------
# The mutation. This is the test that makes the two above mean something.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("site_id", _DRIVEN)
def test_removing_the_conversion_at_this_site_reddens_its_own_test(site_id):
    """Neutralise the conversion at this one line; the transient case must stop
    surfacing ``TransientError``.

    If it still surfaces, the driver above is reaching some other site and the
    pair of tests for this one proves nothing about it — which is the exact state
    eight of these thirteen sites were in.
    """
    case = SITES[site_id]
    cause = case.transient[0]
    assert site_id in _LINES, (
        f"{site_id} is registered but no longer present in the source, so there "
        "is nothing to neutralise — see test_every_discovered_site_has_an_entry"
    )
    with neutralised(site_id, _LINES[site_id], _HELPERS[site_id]):
        try:
            case.drive(cause)
        except TransientError as leaked:
            pytest.fail(
                f"{site_id}: with its conversion neutralised the driver still "
                f"raised TransientError ({leaked}), so it is exercising a "
                "different site"
            )
        except Exception:
            # The original cause propagating is the expected outcome wherever the
            # handler ends in a bare `raise`.
            pass
