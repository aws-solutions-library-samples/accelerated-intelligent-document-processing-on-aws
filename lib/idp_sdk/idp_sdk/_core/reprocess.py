# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reprocess documents through the stack's reprocess resolver.

The server decides whether a document can be processed again, clears the outputs
the run will produce again and queues it: the same code the web UI's Reprocess
runs. The SDK sends the request and reports each document's result.

A request carries a bounded number of documents, because the resolver has a
fixed time to answer. A larger set is sent as several requests, in order, and
the results are joined: ``idp_common.run_plan.resolver_requests`` holds that
rule, which the MCP reprocess tool follows too.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from idp_common.run_plan.resolver_requests import (
    DOCUMENTS_PER_REQUEST,
    QUEUED,
    reprocess_in_requests,
)
from idp_sdk._core.resolver_invoke import ResolverFunctionError, invoke_api_resolver
from idp_sdk.exceptions import (
    IDPProcessingError,
    IDPValidationError,
    ReprocessRefusedError,
)
from idp_sdk.models import (
    DocumentReprocessResult,
    ReprocessOption,
    ReprocessStepOption,
    RerunStep,
)

#: The nested API resolver stack output that holds the reprocess resolver's ARN.
REPROCESS_OUTPUT = "ReprocessDocumentResolverFunctionArn"


def step_value(step: Union[str, RerunStep]) -> str:
    """The start step as the server reads it.

    Raises:
        IDPValidationError: the value is not a step, an alias or ``resume``.
    """
    from idp_common.run_plan.steps import start_values

    value = step.value if isinstance(step, RerunStep) else str(step)
    if value not in start_values():
        raise IDPValidationError(
            f"'{value}' is not a step a reprocess can start at; "
            f"the steps are {', '.join(start_values())}"
        )
    return value


def skip_values(skip: Iterable[Union[str, RerunStep]]) -> List[str]:
    """The steps to leave out, as the server reads them.

    Raises:
        IDPValidationError: a value is not a step a run can skip.
    """
    from idp_common.run_plan.steps import optional_step_ids

    if isinstance(skip, (str, RerunStep)):
        skip = [skip]
    values = [s.value if isinstance(s, RerunStep) else str(s) for s in skip]
    unknown = [value for value in values if value not in optional_step_ids()]
    if unknown:
        raise IDPValidationError(
            f"{', '.join(repr(value) for value in unknown)} cannot be skipped; "
            f"the steps a run can skip are {', '.join(optional_step_ids())}"
        )
    return values


def _requests(document_ids: Sequence[str]) -> Iterable[List[str]]:
    for start in range(0, len(document_ids), DOCUMENTS_PER_REQUEST):
        yield list(document_ids[start : start + DOCUMENTS_PER_REQUEST])


def _profile_arguments(
    version: Optional[str], revision: Optional[int]
) -> Dict[str, Any]:
    arguments: Dict[str, Any] = {}
    if version:
        arguments["version"] = version
    if revision is not None:
        arguments["revision"] = revision
    return arguments


def _result(entry: Any, step: str) -> DocumentReprocessResult:
    if not isinstance(entry, dict) or not entry.get("objectKey"):
        raise IDPProcessingError(
            f"The reprocess resolver returned a result that cannot be read: "
            f"{entry!r:.200}"
        )
    status = str(entry.get("status") or "")
    return DocumentReprocessResult(
        document_id=entry["objectKey"],
        step=RerunStep(step),
        queued=status == QUEUED,
        status=status,
        start_step=entry.get("startStep") or None,
        reason=entry.get("reason") or None,
        message=entry.get("message") or None,
    )


def reprocess(
    stack_name: str,
    region: Optional[str],
    document_ids: Sequence[str],
    step: str,
    skip: Sequence[str] = (),
    version: Optional[str] = None,
    revision: Optional[int] = None,
) -> List[DocumentReprocessResult]:
    """Ask the server to process documents again; one result per document.

    ``step`` and ``skip`` are values :func:`step_value` and :func:`skip_values`
    returned.

    The first request decides whether the request as a whole is acceptable. If
    the server refuses it, nothing was queued and
    :class:`~idp_sdk.exceptions.ReprocessRefusedError` is raised. A later request
    that the server refuses or fails on is reported on the documents it carried
    (``REFUSED`` or ``FAILED``, with the server's message), because documents of
    the earlier requests are already queued, and the remaining requests are
    still sent.

    Raises:
        ReprocessRefusedError: the server refused the first request.
        IDPResourceNotFoundError: the stack has no reprocess resolver output.
        IDPProcessingError: the first request failed, or a result is unreadable.
        ValueError: an answer has no results. The operations that call this
            report it as ``IDPProcessingError``, as they do any other error.
    """
    arguments = _profile_arguments(version, revision)
    arguments["startStep"] = step
    if skip:
        arguments["skipSteps"] = list(skip)

    def send(request: Dict[str, Any]) -> Any:
        return invoke_api_resolver(
            stack_name, region, REPROCESS_OUTPUT, "reprocessDocument", request
        )

    try:
        entries = reprocess_in_requests(send, document_ids, arguments)
    except ResolverFunctionError as e:
        if e.is_refusal:
            raise ReprocessRefusedError(e.error_message, reason=e.error_type) from e
        raise
    return [_result(entry, step) for entry in entries]


def reprocess_options(
    stack_name: str,
    region: Optional[str],
    document_ids: Sequence[str],
    version: Optional[str] = None,
    revision: Optional[int] = None,
) -> List[ReprocessOption]:
    """For each document: its processing mode and the steps a run can start at.

    Reads only.

    Raises:
        ReprocessRefusedError: the server refused the request.
        IDPResourceNotFoundError: the stack has no reprocess resolver output.
        IDPProcessingError: the request failed, or an answer is unreadable.
    """
    options: List[ReprocessOption] = []
    for keys in _requests(document_ids):
        try:
            answer = invoke_api_resolver(
                stack_name,
                region,
                REPROCESS_OUTPUT,
                "getReprocessOptions",
                {"objectKeys": keys, **_profile_arguments(version, revision)},
            )
        except ResolverFunctionError as e:
            if e.is_refusal:
                raise ReprocessRefusedError(e.error_message, reason=e.error_type) from e
            raise
        if not isinstance(answer, list):
            raise IDPProcessingError(
                "The reprocess resolver returned options that cannot be read: "
                f"{answer!r:.200}"
            )
        for entry in answer:
            options.append(
                ReprocessOption(
                    document_id=entry["objectKey"],
                    mode=entry["mode"],
                    default_step=entry["defaultStep"],
                    steps=[
                        ReprocessStepOption(
                            id=option["id"],
                            available=bool(option["available"]),
                            skippable=bool(option["skippable"]),
                            reason=option.get("reason") or None,
                        )
                        for option in entry.get("steps") or []
                    ],
                )
            )
    return options
