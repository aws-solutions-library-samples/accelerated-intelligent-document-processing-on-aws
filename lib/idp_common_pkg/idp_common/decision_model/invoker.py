# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Call a decision model over the System One API.

One backend is implemented: ``lambda``, a direct ``lambda:InvokeFunction`` whose
event is the System One request and whose result is the response. That is the
direct-invocation contract of the Strands Decider's Lambda deployment. The function
name must start with ``GENAIIDP-``, the convention ``LambdaHook`` uses: every
pipeline function that runs the classification, extraction or assessment service
may invoke ``GENAIIDP-*``, which scripts/tests/test_decision_model_invoke_grants.py
asserts for each one.

Failures come in two kinds, and callers treat them differently:

* :class:`DecisionModelRefused`: the model refused the request, for example because
  its state is over the context window or it carries more images than the
  deployment reads. Sending it again gets the same answer, so it is never retried.
* :class:`DecisionModelUnavailable`: the call did not complete (throttling that
  outlasted the retries, a timeout, a crashed execution environment).

Neither is ever converted into a call to another model. A configuration that
selects a decision model uses only the decision model (GitHub #1656).
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from pydantic import ValidationError

from idp_common.decision_model.wire import SystemOneRequest, SystemOneResponse
from idp_common.lambda_deadline import attempt_cannot_finish, clamp_sleep_to_budgets
from idp_common.timeout_budget import BOTOCORE_TOTAL_MAX_ATTEMPTS

logger = logging.getLogger(__name__)

#: The only backend implemented. ``sagemaker`` and ``bedrock_custom`` are declared
#: on ``ExternalModelConfig`` and refused until they are.
SUPPORTED_BACKENDS = ("lambda",)

#: Function names must carry this prefix, the one the pipeline's invoke grant names.
FUNCTION_NAME_PREFIX = "GENAIIDP-"

#: The memory the Strands Decider's Lambda deployment runs at. Used to meter
#: GB-seconds, which is what Lambda bills; override per configuration.
DEFAULT_FUNCTION_MEMORY_MB = 10240

#: Lambda errors worth another attempt: the request never ran, or ran out of room.
_RETRYABLE_CLIENT_ERRORS = frozenset(
    {
        "TooManyRequestsException",
        "ServiceException",
        "EC2ThrottledException",
        "ResourceConflictException",
        "ResourceNotReadyException",
    }
)

#: A function error whose ``errorType`` starts with this is the execution
#: environment failing (the model did not load, the process exited), not the model
#: refusing the request. Lambda replaces the environment, so one more attempt can
#: succeed.
_RUNTIME_ERROR_PREFIX = "Runtime."


class DecisionModelError(Exception):
    """A decision-model call that produced no answer."""


class DecisionModelRefused(DecisionModelError):
    """The model refused the request. Deterministic: never retried."""


class DecisionModelUnavailable(DecisionModelError):
    """The call did not complete."""


def function_name(endpoint: str) -> str:
    """The bare function name of a function ARN, a qualified ARN, or a name.

    Metering keys on it, because an ARN embeds the account and region and so could
    never be a shipped pricing key.
    """
    ref = endpoint.strip()
    if ":function:" in ref:
        ref = ref.split(":function:", 1)[1]
    return ref.split(":", 1)[0]


@dataclass
class DecisionResult:
    """One answered request, with the metering it adds."""

    response: SystemOneResponse
    metering: Dict[str, Dict[str, float]] = field(default_factory=dict)
    duration_seconds: float = 0.0


def merge_metering(results: Sequence[DecisionResult]) -> Dict[str, Dict[str, float]]:
    """Sum the metering of several results."""
    merged: Dict[str, Dict[str, float]] = {}
    for result in results:
        for key, units in result.metering.items():
            target = merged.setdefault(key, {})
            for unit, value in units.items():
                target[unit] = target.get(unit, 0) + value
    return merged


class DecisionModelClient:
    """A System One client for one configured endpoint.

    Thread-safe: :meth:`decide_many` shares one boto3 client across worker threads.
    """

    def __init__(
        self,
        endpoint: str,
        backend: str = "lambda",
        timeout_ms: int = 30000,
        max_concurrency: int = 4,
        function_memory_mb: int = DEFAULT_FUNCTION_MEMORY_MB,
        max_retries: int = 4,
        region: Optional[str] = None,
        lambda_client: Any = None,
        metrics_enabled: bool = True,
    ):
        backend = (backend or "lambda").strip().lower()
        if backend not in SUPPORTED_BACKENDS:
            raise ValueError(
                f"decision model backend {backend!r} is not implemented; "
                f"supported: {', '.join(SUPPORTED_BACKENDS)}"
            )
        if not endpoint or not endpoint.strip():
            raise ValueError("decision model endpoint is empty")
        name = function_name(endpoint)
        if not name.startswith(FUNCTION_NAME_PREFIX):
            raise ValueError(
                f"decision model function {name!r} must be named with the "
                f"{FUNCTION_NAME_PREFIX} prefix: the pipeline functions may invoke "
                "only functions named with it"
            )
        self.endpoint = endpoint.strip()
        self.function_name = name
        self.timeout_seconds = max(1.0, timeout_ms / 1000.0)
        self.max_concurrency = max(1, int(max_concurrency))
        self.function_memory_mb = int(function_memory_mb)
        self.max_retries = max(0, int(max_retries))
        self.metrics_enabled = metrics_enabled
        # Bounds the calls in flight across every thread using this client, so
        # ``max_concurrency`` holds however many workers the caller runs.
        self._in_flight = threading.BoundedSemaphore(self.max_concurrency)
        if lambda_client is None:
            # botocore's own retries are off: an attempt re-runs paid work, so
            # retrying belongs to this class, which knows which errors are worth it.
            lambda_client = boto3.client(
                "lambda",
                region_name=region,
                config=Config(
                    connect_timeout=10,
                    read_timeout=self.timeout_seconds,
                    retries={
                        "total_max_attempts": BOTOCORE_TOTAL_MAX_ATTEMPTS,
                        "mode": "standard",
                    },
                    max_pool_connections=max(10, self.max_concurrency),
                ),
            )
        self._lambda = lambda_client

    def metering_key(self, context: str) -> str:
        return f"{context}/decision_model/{self.function_name}"

    def decide(self, request: SystemOneRequest, context: str) -> DecisionResult:
        """Send one request and return its answers.

        Raises:
            DecisionModelRefused: the model refused the request.
            DecisionModelUnavailable: the call did not complete.
        """
        payload = json.dumps(request.model_dump(mode="json")).encode("utf-8")
        worst_attempt_seconds = 0.0
        attempt = 0
        while True:
            # Bound before the try: the handler below reads it, and a binding only
            # inside the try would turn an early failure into UnboundLocalError.
            # Rebound once the concurrency slot is held, so neither the gate nor the
            # metered GB-seconds count time spent waiting for a slot.
            attempt_start_time = time.time()
            try:
                with self._in_flight:
                    attempt_start_time = time.time()
                    return self._invoke_once(payload, context, attempt_start_time)
            except _Retry as retry:
                worst_attempt_seconds = max(
                    worst_attempt_seconds, time.time() - attempt_start_time
                )
                if attempt >= self.max_retries:
                    self._put_metric("DecisionModelRequestsFailed", 1)
                    raise DecisionModelUnavailable(
                        f"{self.function_name}: {retry.reason} after "
                        f"{attempt + 1} attempts"
                    ) from None
                backoff = self._backoff_within_invocation(attempt)
                if attempt_cannot_finish(
                    worst_attempt_seconds + backoff, self.function_name
                ):
                    self._put_metric("DecisionModelRequestsFailed", 1)
                    self._put_metric("DecisionModelRetryAbandonedOutOfTime", 1)
                    raise DecisionModelUnavailable(
                        f"{self.function_name}: {retry.reason}; no time left in "
                        "this invocation for another attempt"
                    ) from None
                logger.warning(
                    "Decision model %s: %s; retrying in %.1fs (attempt %d of %d)",
                    self.function_name,
                    retry.reason,
                    backoff,
                    attempt + 2,
                    self.max_retries + 1,
                )
                time.sleep(backoff)
                attempt += 1
            except DecisionModelError:
                # Every terminal failure that is not a retry (a refusal, a
                # non-retryable Lambda error, a read timeout, a response that is not
                # System One) records DecisionModelRequestsFailed exactly once, as
                # retry exhaustion does above, so refusals show as failures rather
                # than as absent traffic.
                self._put_metric("DecisionModelRequestsFailed", 1)
                raise

    @staticmethod
    def _backoff_within_invocation(attempt: int) -> float:
        """Exponential backoff with jitter, shortened to stay inside the invocation."""
        return clamp_sleep_to_budgets(
            min(30.0, 2.0 * (2**attempt)) + random.uniform(0, 1)
        )

    def _put_metric(self, metric_name: str, value: float, unit: str = "Count") -> None:
        """Publish a metric, never failing the call that reports it."""
        if not self.metrics_enabled:
            return
        try:
            from idp_common.metrics import put_metric

            put_metric(metric_name, value, unit)
        except Exception as e:  # noqa: BLE001 - a metric must not fail the request
            logger.warning("Could not publish metric %s: %s", metric_name, e)

    def decide_many(
        self, requests: Sequence[SystemOneRequest], context: str
    ) -> List[DecisionResult]:
        """:meth:`decide` for each request, at most ``max_concurrency`` at once.

        Results are in request order. The first failure is raised once every
        request has finished, so no call is abandoned mid-flight.
        """
        if not requests:
            return []
        if len(requests) == 1 or self.max_concurrency == 1:
            return [self.decide(r, context) for r in requests]
        with ThreadPoolExecutor(
            max_workers=min(self.max_concurrency, len(requests))
        ) as pool:
            futures = [pool.submit(self.decide, r, context) for r in requests]
        errors = [f.exception() for f in futures if f.exception() is not None]
        if errors:
            raise errors[0]
        return [f.result() for f in futures]

    def _invoke_once(
        self, payload: bytes, context: str, attempt_start_time: float
    ) -> DecisionResult:
        try:
            response = self._lambda.invoke(
                FunctionName=self.endpoint,
                InvocationType="RequestResponse",
                Payload=payload,
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in _RETRYABLE_CLIENT_ERRORS:
                raise _Retry(code) from None
            raise DecisionModelUnavailable(
                f"{self.function_name}: {code}: "
                f"{e.response.get('Error', {}).get('Message', '')}"
            ) from e
        except ReadTimeoutError as e:
            # The invocation may still be running and billing; another attempt
            # would start the same work again. Fail instead.
            raise DecisionModelUnavailable(
                f"{self.function_name}: no answer within {self.timeout_seconds:.0f}s "
                "(the configured timeout_ms)"
            ) from e
        except (ConnectTimeoutError, EndpointConnectionError) as e:
            # The connection never opened, so the invocation never began: the one
            # failure that is safe to retry without re-running paid work.
            raise _Retry(f"{type(e).__name__}: {e}") from None
        except BotoCoreError as e:
            # Anything else below the API, including ConnectionClosedError: the
            # request may have been delivered, so like a read timeout it is not
            # retried, but it is still this client's failure type.
            raise DecisionModelUnavailable(
                f"{self.function_name}: {type(e).__name__}: {e}"
            ) from e

        duration = time.time() - attempt_start_time
        body = response["Payload"].read().decode("utf-8")
        function_error = response.get("FunctionError")
        if function_error:
            try:
                error = json.loads(body)
            except ValueError:
                error = {"errorMessage": body}
            error_type = str(error.get("errorType", ""))
            message = str(error.get("errorMessage", body))
            if error_type.startswith(_RUNTIME_ERROR_PREFIX):
                raise _Retry(f"{error_type}: {message}")
            # A ValueError or ValidationError raised by the model's handler: the
            # request is over the window, over the request limit, or malformed.
            raise DecisionModelRefused(
                f"{self.function_name} refused the request: {error_type}: {message}"
            )

        try:
            parsed = SystemOneResponse.model_validate_json(body)
        except ValidationError as e:
            raise DecisionModelUnavailable(
                f"{self.function_name} returned a response that is not a System One "
                f"response: {e.errors(include_url=False)[:3]}"
            ) from e

        logger.info(
            "Decision model %s answered %d question(s): %d input tokens, %.2fs",
            self.function_name,
            len(parsed.answers),
            parsed.usage.input_tokens,
            duration,
        )
        metering = {
            self.metering_key(context): {
                "requests": 1,
                # What Lambda bills. Measured at the caller, so it includes the
                # round trip and slightly over-states the billed duration.
                "gb_seconds": round(duration * self.function_memory_mb / 1024.0, 6),
            }
        }
        return DecisionResult(
            response=parsed, metering=metering, duration_seconds=duration
        )


class _Retry(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
