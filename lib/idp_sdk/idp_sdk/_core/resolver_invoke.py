# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Call one of a stack's API resolver functions directly.

The web UI reaches the API resolvers through the REST API, with a signed-in
user. The SDK has no user: it invokes the resolver Lambda with the caller's own
AWS credentials, and the event carries no ``identity``. A resolver that accepts
such an event treats permission to invoke the function as the authorisation, so
the server does the work and the SDK holds no copy of it.

Each resolver's ARN is an output of the nested API resolver stack.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from idp_common.run_plan.resolver_requests import ResolverCallError
from idp_sdk._core.region import resolve_region
from idp_sdk._core.stack_info import StackInfo
from idp_sdk.exceptions import (
    IDPConfigurationError,
    IDPProcessingError,
    IDPResourceNotFoundError,
)

logger = logging.getLogger(__name__)

#: Outputs added after a release: a stack deployed from that release, or from an
#: earlier one, does not have them. The message for a missing output names the
#: release, so the reader knows the stack has to be updated and that nothing is
#: wrong with the request.
_OUTPUTS_ADDED_AFTER = {
    "ReprocessDocumentResolverFunctionArn": "0.6.15",
    "ReviewQueuesResolverFunctionArn": "0.6.15",
}

#: Seconds the client waits for a function's answer. Longer than the timeout of
#: the resolver functions this module is used for (the reprocess resolver has the
#: longest, 60 seconds), so a slow call ends with the function's own timeout. A
#: client that gives up first retries, and the retry would run a request that
#: queues documents a second time.
_READ_TIMEOUT_SECONDS = 120

#: Region to Lambda client, so the requests of one operation share a client.
_LAMBDA_CLIENTS: Dict[Optional[str], Any] = {}

#: (stack, region, output) to function ARN. One operation can take several
#: requests (a batch is sent in parts, a list is read page by page), and each
#: lookup is two CloudFormation calls.
_FUNCTION_ARNS: Dict[Tuple[str, Optional[str], str], str] = {}


class ResolverFunctionError(IDPProcessingError, ResolverCallError):
    """The resolver function raised. Carries what it raised.

    ``error_type`` is the name of the exception class in the function
    (``PermissionError`` for a refusal, ``ValueError`` for a bad argument) and
    ``error_message`` its text; ``is_refusal`` says whether the function refused
    the request rather than failed on it.
    """

    def __init__(self, field_name: str, error_type: str, error_message: str):
        self.field_name = field_name
        ResolverCallError.__init__(self, error_type, error_message)
        self.args = (f"{field_name} failed: {error_message}",)


def api_resolver_stack_output(stack_info: StackInfo, output_key: str) -> Optional[str]:
    """An output of the stack's nested API resolver stack.

    The nested stack's logical id is ``APIRESOLVERSTACK``. Stacks deployed from
    templates that still used AWS AppSync call it ``APPSYNCSTACK``, so both are
    tried.

    Raises:
        ValueError: neither nested stack has the output.
    """
    try:
        return stack_info.get_nested_stack_output(
            nested_stack_pattern="apiresolver",
            output_key=output_key,
        )
    except ValueError:
        return stack_info.get_nested_stack_output(
            nested_stack_pattern="appsync",
            output_key=output_key,
        )


def _missing_output(stack_name: str, output_key: str) -> str:
    message = (
        f"Stack '{stack_name}' has no output '{output_key}' in its API resolver stack."
    )
    added_after = _OUTPUTS_ADDED_AFTER.get(output_key)
    if added_after:
        message += (
            f" The output was added after release {added_after}: update the stack "
            "to a later release to use this operation."
        )
    return message


def _region(region: Optional[str]) -> str:
    """The Region the stack is looked up and its function invoked in.

    Worked out the one way the SDK and CLI do (``idp_sdk.resolve_region``): the
    Region given; else the one a client built with no Region reaches; else
    ``AWS_REGION``, which boto3 itself does not read. The answer is passed to
    every client here, so a shell that sets only ``AWS_REGION`` reaches its stack.

    Raises:
        IDPConfigurationError: no source names a Region.
    """
    resolved = resolve_region(region)
    if not resolved:
        raise IDPConfigurationError(
            "Region could not be determined. Pass a region, or set "
            "AWS_DEFAULT_REGION, AWS_REGION or a region in your AWS profile."
        )
    return resolved


def _lambda_client(region: Optional[str]) -> Any:
    if region not in _LAMBDA_CLIENTS:
        _LAMBDA_CLIENTS[region] = boto3.client(
            "lambda",
            region_name=region,
            config=Config(read_timeout=_READ_TIMEOUT_SECONDS),
        )
    return _LAMBDA_CLIENTS[region]


def forget_stack_lookups() -> None:
    """Drop the cached clients and function ARNs (a test starts from nothing)."""
    _LAMBDA_CLIENTS.clear()
    _FUNCTION_ARNS.clear()


def _function_arn(stack_name: str, region: Optional[str], output_key: str) -> str:
    """The resolver function's ARN, looked up once per stack, region and output."""
    key = (stack_name, region, output_key)
    if key not in _FUNCTION_ARNS:
        stack_info = StackInfo(stack_name=stack_name, region=region)
        try:
            function_arn = api_resolver_stack_output(stack_info, output_key)
        except ValueError as e:
            raise IDPResourceNotFoundError(
                _missing_output(stack_name, output_key)
            ) from e
        if not function_arn:
            raise IDPResourceNotFoundError(_missing_output(stack_name, output_key))
        _FUNCTION_ARNS[key] = function_arn
    return _FUNCTION_ARNS[key]


def invoke_api_resolver(
    stack_name: str,
    region: Optional[str],
    output_key: str,
    field_name: str,
    arguments: Dict[str, Any],
) -> Any:
    """Invoke an API resolver function and return what it returns.

    Args:
        stack_name: the main stack.
        region: the stack's region; ``None`` is resolved as every SDK call
            resolves it (``idp_sdk.resolve_region``).
        output_key: the nested API resolver stack output that holds the
            function's ARN.
        field_name: the operation, as the resolver reads it from
            ``info.fieldName``.
        arguments: the operation's arguments, named as in the API schema.

    Raises:
        IDPConfigurationError: no Region was given and none can be resolved.
        IDPResourceNotFoundError: the stack has no such output.
        ResolverFunctionError: the function raised.
        IDPProcessingError: the function could not be invoked or its answer
            could not be read.
    """
    region = _region(region)
    function_arn = _function_arn(stack_name, region, output_key)
    payload = {"info": {"fieldName": field_name}, "arguments": arguments}
    try:
        response = _lambda_client(region).invoke(
            FunctionName=function_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        body = response["Payload"].read()
        result = json.loads(body) if body else None
    except (ClientError, BotoCoreError) as e:
        # The function may have been replaced by a stack update: look it up
        # again next time.
        _FUNCTION_ARNS.pop((stack_name, region, output_key), None)
        raise IDPProcessingError(f"Could not invoke {field_name}: {e}") from e
    except ValueError as e:
        raise IDPProcessingError(
            f"{field_name} returned an answer that is not JSON"
        ) from e

    if response.get("FunctionError"):
        error = result if isinstance(result, dict) else {}
        raise ResolverFunctionError(
            field_name,
            str(error.get("errorType") or "Error"),
            str(error.get("errorMessage") or "the function failed"),
        )
    logger.debug("Invoked %s on %s", field_name, function_arn)
    return result
