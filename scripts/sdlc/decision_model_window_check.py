#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Live window check: every decision-model request the pipeline builds fits the
deployed decider's input window (``make stacktest-decision-window``; #2051).

Why it exists. ``idp_common.decision_model.budget`` cuts each request's text to
``max_input_tokens`` with a character-based ESTIMATE, against constants that
describe the remote model (``CONTEXT_WINDOW_TOKENS``, ``PROMPT_RESERVE_TOKENS``,
``IMAGE_TOKENS``). The unit suites check that callers cut to that estimate; only
the deployed model can say whether the estimate and the constants still hold.
This check asks it. It is live and opt-in: in neither CI, and not a merge gate.

WHAT IT SENDS
-------------
  1. Calibration probes, built here, with a state of digits (the decider's
     tokenizer reads each digit as one token):
       * a short request, to measure what the decider adds around the state,
         and the same with its question twice, to measure one question;
       * a state that fills the window to just under ``CONTEXT_WINDOW_TOKENS``,
         which must be answered, and one just over it, which must be refused;
       * the state just under the window again with two questions, which must
         be answered;
       * the same short request with one square page image at the side the
         decider reads images at, and one with a portrait page image, to measure
         what an image costs.
  2. The largest request each caller builds, made by the callers' own code at the
     configuration's largest ``max_pages`` and ``max_input_tokens`` (its cap):
       * section classification (``DecisionModelSectionLabeler.build_request``),
         eight described classes, in ``text``, ``both`` and ``images``;
       * extraction confidence (``DecisionModelConfidenceScorer.score``), a
         field with a value and an empty one, one question a request, in
         ``text``, ``both`` and ``images``.
     Text modes are run with three kinds of dense page text (a table of figures,
     CJK, plain words), each well over the window, so every one is cut.

ASSERTIONS (exit 1 when any fails)
----------------------------------
  * Every caller request is answered: none is refused for its size.
  * Every answered request's measured ``usage.input_tokens`` is within its
    estimate plus ``PROMPT_RESERVE_TOKENS``. The estimate is the formula the unit
    suites' fake deciders enforce (``window_contract.request_tokens``: state,
    images and the longest question with its criteria), which is what the
    callers' cut is checked against offline. (A request over the window itself is the edge probes' finding:
    the decider refuses one.)
  * The window-edge probes: the one under the window is answered, and the one
    over it is refused as a request error (not a crashed environment). Either
    outcome reversed means ``CONTEXT_WINDOW_TOKENS`` no longer describes the
    model. Two questions beside a state that fits one are answered too: the
    window holds the state and the longest question, as ``state_budget``
    assumes, not every question.
  * What the decider adds around the state is within ``PROMPT_RESERVE_TOKENS``,
    and what each probe image costs is within ``IMAGE_TOKENS``.

Reported for every request: the estimate, the measured input tokens, their
difference, the latency and the outcome, in ``<report dir>/report.json``.

INSTALLING THE DECIDER
----------------------
``--endpoint`` names a decider function or alias directly. ``--stack-name``
names an IDP host stack instead, and the endpoint is read from the output
``DeciderEndpoint`` of its extension stack
``<stack>-feature-decision-model-decider``. ``--install`` first installs the
extension on that host from this checkout (``idp-feature-cli deploy
--from-code``), and refuses when the host already has it. ``--uninstall``, which
needs ``--install``, deletes the extension stack afterwards, pass or fail, and
only when this run created it. Neither touches the host stack. An uninstall that
fails is reported and makes a passing run exit 2.

COST
----
The decider runs at 10,240 MB and is billed by duration; a request near the
window takes up to about a minute, and the first request to a new execution
environment also loads the model. A full run sends a few dozen requests, one at
a time.

EXIT CODES: 0 PASS, 1 FAIL, 2 could not run (no endpoint, setup error).

USAGE
-----
  make stacktest-decision-window STACK_NAME=<host> [INSTALL=1] [UNINSTALL=1]
  make stacktest-decision-window ENDPOINT=<function or alias ARN>
  make stacktest-decision-window DRY_RUN=1      # build and estimate only, no AWS
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSION_DIR = REPO_ROOT / "feature-platform" / "decision-model-decider"
EXTENSION_STACK_SUFFIX = "-feature-decision-model-decider"
ENDPOINT_OUTPUT = "DeciderEndpoint"

#: How far either side of the window the edge probes sit, in tokens.
EDGE_MARGIN_TOKENS = 16

#: The decider's first request to a new execution environment loads the model,
#: which takes minutes after a deploy; requests near the window take up to about
#: a minute. Each call gets this long.
CALL_TIMEOUT_MS = 600_000

#: The classes section classification is asked to choose between: eight, each
#: with a description of a realistic length, so the question is not trivially
#: small beside the state.
SECTION_CLASSES = {
    f"class{i}": (
        f"Document type {i}: a single-purpose financial or identity document whose "
        "first page carries a printed title, an issuer name and address block, a "
        "date, and a table of line items or a set of labelled fields."
    )
    for i in range(1, 9)
}

#: The fields extraction confidence is asked about: one with a value, one empty,
#: because the two use different question templates.
CONFIDENCE_FIELDS = {
    "account_number": (
        "0123-4567-8901",
        "The account number printed in the statement header, usually beside the "
        "account holder's name, formatted as groups of digits.",
    ),
    "closing_balance": (
        None,
        "The closing balance at the end of the statement period, as printed in "
        "the account summary table, including its currency.",
    ),
}

#: The page text shapes sent in the text modes. Each is well over the window on
#: its own, so every one is cut to the cap; what differs is how many real tokens
#: the cut text costs against its estimate. ``table`` (a markdown statement table,
#: as OCR emits for tabular pages) and ``codes`` (alphanumeric serials) are the
#: densest measured against the pinned model's tokenizer, about 1.18 tokens per
#: estimated token; ``figures`` about 1.12; ``cjk`` and ``words`` below 1 (#2051).
DENSE_STYLES = ("figures", "table", "codes", "cjk", "words")

#: The characters of the ``codes`` style's serials: capitals and digits, without
#: the letters serial schemes leave out (I, O, Q).
SERIAL_ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"


def serial(seed: int, length: int = 17) -> str:
    """A deterministic alphanumeric serial, such as a vehicle or device id."""
    out = []
    for _ in range(length):
        seed = (seed * 1103515245 + 12345) % 2147483648
        out.append(SERIAL_ALPHABET[(seed >> 16) % len(SERIAL_ALPHABET)])
    return "".join(out)


def dense_page(style: str, page: int) -> str:
    """One page of ``style`` OCR-like text, several thousand estimated tokens."""
    if style == "figures":
        return "\n".join(
            f"{page:02d}-{i:05d} 2026-11-{i % 28 + 1:02d} "
            f"{i * 7919 % 100000:>9,}.{i % 100:02d} | {i * 31 % 997:03d}"
            for i in range(150)
        )
    if style == "table":
        rows = ["| Date | Amount | Balance |", "|---|---|---|"]
        rows += [
            f"| {i % 28 + 1:02d}/11/2026 | {i * 7919 % 100000:,}.{i % 100:02d} | "
            f"{(i + page) * 9973 % 1000000:,}.{i * 7 % 100:02d} |"
            for i in range(150)
        ]
        return "\n".join(rows)
    if style == "codes":
        return "\n".join(serial(page * 7919 + i) for i in range(400))
    if style == "cjk":
        return "\n".join(
            f"第{page}頁 {i}行 請求書の合計金額と明細行" * 3 for i in range(70)
        )
    if style == "words":
        return "\n".join(
            f"Page {page} line {i}: " + "statement carried forward balance " * 12
            for i in range(24)
        )
    raise ValueError(f"unknown dense style {style!r}")


def page_image(width: int, height: int, label: str) -> bytes:
    """A PNG page of the given size with lines of text on it."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    step = max(12, height // 40)
    for row, y in enumerate(range(step, height - step, step)):
        draw.text((step, y), f"{label} line {row}: 2026-11-20 1,234.56", fill="black")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def estimated_tokens(request: Dict[str, Any]) -> float:
    """The window a request needs, estimated as the unit suites' fakes estimate it.

    The state, every image at ``IMAGE_TOKENS``, and the longest question with its
    criteria (``lib/idp_common_pkg/tests/unit/decision_model/window_contract.py``
    is the same formula). The decider's own prompt is not in it; that is what
    ``PROMPT_RESERVE_TOKENS`` is for.
    """
    from idp_common.decision_model import IMAGE_TOKENS, estimate_tokens

    def question(q: Dict[str, Any]) -> float:
        criteria = q.get("criteria") or {}
        if isinstance(criteria, dict):
            parts = [f"{name} {_text(text)}" for name, text in criteria.items()]
        else:
            parts = [_text(level) for level in criteria]
        return estimate_tokens("\n".join([_text(q.get("instructions")), *parts]))

    questions = request.get("questions") or {}
    longest = max((question(q) for q in questions.values()), default=0.0)
    images = len(request.get("images") or [])
    return (
        estimate_tokens(_text(request.get("state"))) + images * IMAGE_TOKENS + longest
    )


@dataclass
class Case:
    """One request to send, and what is expected of it."""

    name: str
    kind: str  # "caller" | "edge-fit" | "edge-over" | "calibration"
    request: Dict[str, Any]
    estimate: float = 0.0
    #: Filled in when sent.
    outcome: str = ""  # "answered" | "refused" | "unavailable"
    input_tokens: Optional[int] = None
    seconds: Optional[float] = None
    error: str = ""
    failures: List[str] = field(default_factory=list)


class _CapturingLambda:
    """A Lambda client stand-in that captures the payload and answers nothing.

    The callers' request builders run as they do in the pipeline; this records
    the request a caller hands its client, so it can be sent on separately.
    """

    def __init__(self) -> None:
        self.payloads: List[Dict[str, Any]] = []

    def invoke(self, FunctionName, InvocationType, Payload):  # noqa: N803 - boto3
        request = json.loads(Payload)
        self.payloads.append(request)
        answers = {
            name: {"type": "noul", "noul": 0.5} for name in request.get("questions", {})
        }
        body = {"model": "capture", "answers": answers}
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(body).encode())}


# A fake endpoint for the in-process capture client: it is never invoked.
CAPTURE_ENDPOINT = "arn:aws:lambda:us-east-1:111122223333:function:GENAIIDP-capture"  # arn-partition-ok: fake, never invoked


def section_cases(png: bytes) -> List[Case]:
    """The largest section-classification request in each input mode."""
    from idp_common.classification.decision_model_labeler import (
        DecisionModelSectionLabeler,
    )
    from idp_common.classification.service import ClassificationService
    from idp_common.config.models import (
        DECISION_MODEL_MAX_INPUT_TOKENS,
        DECISION_MODEL_MAX_SECTION_PAGES,
    )

    pages = DECISION_MODEL_MAX_SECTION_PAGES
    cases: List[Case] = []
    for mode in ("text", "both", "images"):
        config = {
            "classes": [
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": name,
                    "x-aws-idp-document-type": name,
                    "type": "object",
                    "description": description,
                    "properties": {"title": {"type": "string"}},
                }
                for name, description in SECTION_CLASSES.items()
            ],
            "classification": {
                "strategy": "split_then_classify",
                "split_then_classify": {
                    "section_classification": {
                        "method": "decision_model",
                        "pages": "all",
                        "decision_model": {
                            "endpoint": CAPTURE_ENDPOINT,
                            "input": mode,
                            "max_pages": pages,
                            "max_input_tokens": DECISION_MODEL_MAX_INPUT_TOKENS,
                        },
                    }
                },
            },
        }
        service = ClassificationService(region="us-east-1", config=config)
        section_cfg = service.config.classification.split_then_classify
        labeler = DecisionModelSectionLabeler(
            service, section_cfg.section_classification
        )

        def load(document, page_ids, stage=None):
            return [(page_id, png) for page_id in page_ids]

        service._load_packet_page_images = load  # type: ignore[method-assign]
        page_ids = [str(p) for p in range(1, pages + 1)]
        for style in DENSE_STYLES if mode != "images" else ("none",):
            contents = (
                {pid: dense_page(style, int(pid)) for pid in page_ids}
                if mode != "images"
                else {}
            )
            request = labeler.build_request(None, contents, page_ids)  # type: ignore[arg-type]
            cases.append(
                Case(
                    name=f"section {mode} {style} x{pages}p",
                    kind="caller",
                    request=request.model_dump(mode="json"),
                )
            )
    return cases


def confidence_cases(png: bytes) -> List[Case]:
    """The largest extraction-confidence request in each input mode.

    Each confidence request carries one page, so ``max_pages`` (set to its
    largest) decides how many requests a section makes, not how large one is.
    One page per style is therefore the largest request there is.

    ``max_questions_per_request`` is 1, so each request holds one field's
    question. The window holds the state and the LONGEST question, while the
    decider's ``usage.input_tokens`` counts the state once and EVERY question, so
    only a one-question request's usage is the quantity the window bounds. The
    request with the longest question is the largest either way.
    """
    from idp_common.assessment.decision_model_confidence import (
        DecisionModelConfidenceScorer,
    )
    from idp_common.config.models import (
        DECISION_MODEL_MAX_CONFIDENCE_PAGES,
        DECISION_MODEL_MAX_INPUT_TOKENS,
        ConfidenceConfig,
    )
    from idp_common.decision_model.invoker import DecisionModelClient

    schema = {
        "type": "object",
        "properties": {
            name: {"type": "string", "description": description}
            for name, (_, description) in CONFIDENCE_FIELDS.items()
        },
    }
    values = {name: value for name, (value, _) in CONFIDENCE_FIELDS.items()}
    cases: List[Case] = []
    for mode in ("text", "both", "images"):
        cfg = ConfidenceConfig.model_validate(
            {
                "mode": "separate",
                "method": "decision_model",
                "decision_model": {
                    "endpoint": CAPTURE_ENDPOINT,
                    "input": mode,
                    "max_pages": DECISION_MODEL_MAX_CONFIDENCE_PAGES,
                    "max_input_tokens": DECISION_MODEL_MAX_INPUT_TOKENS,
                    "max_questions_per_request": 1,
                },
            }
        )
        scorer = DecisionModelConfidenceScorer(cfg, region="us-east-1")
        for style in DENSE_STYLES if mode != "images" else ("none",):
            capture = _CapturingLambda()
            scorer.client = DecisionModelClient(
                CAPTURE_ENDPOINT,
                max_concurrency=1,
                lambda_client=capture,
                metrics_enabled=False,
            )
            text = dense_page(style, 1) if mode != "images" else ""
            scorer.score(values, schema, text, [png], [text])
            for index, request in enumerate(capture.payloads):
                cases.append(
                    Case(
                        name=f"confidence {mode} {style} request {index + 1}",
                        kind="caller",
                        request=request,
                    )
                )
    return cases


PROBE_QUESTION = {
    "probe": {"type": "noul", "instructions": "Is the text a sequence of digits?"}
}

#: Digits in the short calibration state.
CALIBRATION_DIGITS = 200


def digits(count: int) -> str:
    """``count`` digits: the decider's tokenizer reads each digit as one token."""
    return ("0123456789" * (count // 10 + 1))[:count]


def probe(
    name: str,
    kind: str,
    state_digits: int,
    images: Tuple[bytes, ...] = (),
    questions: int = 1,
):
    from idp_common.decision_model.wire import SystemOneRequest

    (question,) = PROBE_QUESTION.values()
    request = SystemOneRequest.model_validate(
        {
            "state": digits(state_digits),
            "questions": {f"probe{i}": question for i in range(questions)},
            "images": [_b64(image) for image in images],
        }
    )
    return Case(name=name, kind=kind, request=request.model_dump(mode="json"))


def _b64(image: bytes) -> str:
    from idp_common.decision_model import encode_image

    return encode_image(image)


@dataclass
class Report:
    endpoint: str
    region: str
    constants: Dict[str, Any]
    measured: Dict[str, Any] = field(default_factory=dict)
    cases: List[Case] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        out = asdict(self)
        for case in out["cases"]:
            request = case.pop("request")
            case["state_characters"] = len(_text(request.get("state")))
            case["images"] = len(request.get("images") or [])
            case["questions"] = len(request.get("questions") or {})
        return out


Sender = Callable[[Case], None]


def live_sender(endpoint: str, region: str) -> Sender:
    """Sends a case to the endpoint with the pipeline's own client."""
    from idp_common.decision_model import DecisionModelClient, SystemOneRequest
    from idp_common.decision_model.invoker import (
        DecisionModelRefused,
        DecisionModelUnavailable,
    )

    client = DecisionModelClient(
        endpoint,
        timeout_ms=CALL_TIMEOUT_MS,
        max_concurrency=1,
        max_retries=2,
        region=region,
        metrics_enabled=False,
    )

    def send(case: Case) -> None:
        request = SystemOneRequest.model_validate(case.request)
        start = time.time()
        try:
            result = client.decide(request, "window_check")
        except DecisionModelRefused as e:
            case.outcome, case.error = "refused", str(e)
        except DecisionModelUnavailable as e:
            case.outcome, case.error = "unavailable", str(e)
        else:
            case.outcome = "answered"
            case.input_tokens = result.response.usage.input_tokens
        case.seconds = round(time.time() - start, 2)

    return send


def build_caller_cases() -> List[Case]:
    """Every caller case, built offline by the callers' own code."""
    from idp_common.decision_model.wire import IMAGE_MAX_SIDE

    square = page_image(IMAGE_MAX_SIDE, IMAGE_MAX_SIDE, "square page")
    cases = section_cases(square) + confidence_cases(square)
    for case in cases:
        case.estimate = round(estimated_tokens(case.request), 1)
    return cases


def run_check(send: Sender, report: Report, caller_cases: List[Case]) -> None:
    """Send every probe and caller case, and record each failure on ``report``."""
    from idp_common.decision_model import IMAGE_TOKENS
    from idp_common.decision_model.budget import PROMPT_RESERVE_TOKENS
    from idp_common.decision_model.wire import CONTEXT_WINDOW_TOKENS, IMAGE_MAX_SIDE

    def fail(case: Optional[Case], message: str) -> None:
        if case is not None:
            case.failures.append(message)
        report.failures.append(f"{case.name}: {message}" if case else message)

    # 1. What the decider adds around a state: its prompt and the question.
    short = probe("calibration: short digits", "calibration", CALIBRATION_DIGITS)
    send(short)
    report.cases.append(short)
    if short.outcome != "answered" or not short.input_tokens:
        fail(
            short,
            f"the short calibration request was {short.outcome or 'not sent'} "
            f"({short.error or 'no usage.input_tokens reported'}); nothing else "
            "can be measured",
        )
        return
    overhead = short.input_tokens - CALIBRATION_DIGITS
    question_estimate = estimated_tokens({"questions": PROBE_QUESTION})
    report.measured["overhead_tokens"] = overhead
    report.measured["prompt_tokens"] = round(overhead - question_estimate, 1)
    if overhead - question_estimate > PROMPT_RESERVE_TOKENS:
        fail(
            short,
            f"the decider adds about {overhead - question_estimate:.0f} tokens of "
            f"its own prompt, over PROMPT_RESERVE_TOKENS ({PROMPT_RESERVE_TOKENS})",
        )

    # What one more copy of the question adds to usage.input_tokens, which counts
    # every question while the window holds only the longest.
    double = probe(
        "calibration: short digits, two questions",
        "calibration",
        CALIBRATION_DIGITS,
        questions=2,
    )
    send(double)
    report.cases.append(double)
    question_cost = (double.input_tokens or 0) - short.input_tokens
    report.measured["question_tokens"] = question_cost
    # What the decider adds once per request, beside the question's own share.
    report.measured["fixed_prompt_tokens"] = overhead - question_cost
    if double.outcome != "answered" or question_cost < 2:
        fail(
            double,
            f"a second question was {double.outcome} and added {question_cost} "
            "tokens, so the per-question window cannot be probed",
        )

    # 2. The window's edge, either side of CONTEXT_WINDOW_TOKENS.
    room = CONTEXT_WINDOW_TOKENS - overhead
    fit = probe(
        f"edge: {EDGE_MARGIN_TOKENS} tokens under the window",
        "edge-fit",
        room - EDGE_MARGIN_TOKENS,
    )
    over = probe(
        f"edge: {EDGE_MARGIN_TOKENS} tokens over the window",
        "edge-over",
        room + EDGE_MARGIN_TOKENS,
    )
    # The window holds the state and the longest question, not every question:
    # the budget's rule. Two questions beside a state that fits one must pass.
    # The margin is under half a question, so a window counting both refuses it.
    pair_margin = max(1, min(EDGE_MARGIN_TOKENS, question_cost // 2))
    pair = probe(
        f"edge: two questions, {pair_margin} under the window",
        "edge-fit",
        room - pair_margin,
        questions=2,
    )
    for case in (fit, over, pair):
        send(case)
        report.cases.append(case)
    if fit.outcome != "answered":
        fail(
            fit,
            f"a request measured at about {CONTEXT_WINDOW_TOKENS - EDGE_MARGIN_TOKENS}"
            f" tokens was {fit.outcome} ({fit.error}): the window is smaller than "
            f"CONTEXT_WINDOW_TOKENS ({CONTEXT_WINDOW_TOKENS})",
        )
    if over.outcome != "refused":
        fail(
            over,
            f"a request of about {CONTEXT_WINDOW_TOKENS + EDGE_MARGIN_TOKENS} tokens "
            f"was {over.outcome}, not refused: the window is larger than "
            f"CONTEXT_WINDOW_TOKENS ({CONTEXT_WINDOW_TOKENS}), or the decider no "
            "longer refuses an oversize request as a request error",
        )
    if pair.outcome != "answered":
        fail(
            pair,
            f"two questions beside a state that fits one were {pair.outcome} "
            f"({pair.error}): the window no longer holds the state and the longest "
            "question only, which is what state_budget assumes",
        )

    # 3. What one page image costs, at the decider's side and as a portrait page.
    images = {
        "square": page_image(IMAGE_MAX_SIDE, IMAGE_MAX_SIDE, "square page"),
        "portrait": page_image(1275, 1650, "portrait page"),
    }
    report.measured["image_tokens"] = {}
    for shape, image in images.items():
        case = probe(
            f"image cost: one {shape} page", "calibration", CALIBRATION_DIGITS, (image,)
        )
        send(case)
        report.cases.append(case)
        if case.outcome != "answered" or not case.input_tokens:
            fail(case, f"the image probe was {case.outcome} ({case.error})")
            continue
        cost = case.input_tokens - short.input_tokens
        report.measured["image_tokens"][shape] = cost
        if cost > IMAGE_TOKENS:
            fail(case, f"one {shape} image costs {cost} tokens, over IMAGE_TOKENS")

    # 4. The callers' largest requests.
    for case in caller_cases:
        send(case)
        report.cases.append(case)
        if case.outcome != "answered":
            fail(case, f"{case.outcome}: {case.error}")
            continue
        if case.input_tokens is None:
            fail(case, "answered without usage.input_tokens")
            continue
        if len(case.request.get("questions") or {}) != 1:
            fail(
                case,
                "carries more than one question, and usage.input_tokens counts "
                "every one, so it cannot be compared with the window",
            )
            continue
        if case.input_tokens > case.estimate + PROMPT_RESERVE_TOKENS:
            fail(
                case,
                f"measured {case.input_tokens} tokens against an estimate of "
                f"{case.estimate:.0f} plus PROMPT_RESERVE_TOKENS "
                f"({PROMPT_RESERVE_TOKENS}): the estimate does not err high",
            )


def extension_stack(host: str) -> str:
    return f"{host}{EXTENSION_STACK_SUFFIX}"


def extension_installed(cloudformation: Any, host: str) -> bool:
    """Whether the host already has the extension stack, in any state."""
    from botocore.exceptions import ClientError

    try:
        cloudformation.describe_stacks(StackName=extension_stack(host))
    except ClientError as e:
        if "does not exist" in str(e):
            return False
        raise
    return True


def resolve_endpoint(cloudformation: Any, host: str) -> str:
    """The extension stack's ``DeciderEndpoint`` output."""
    stack = cloudformation.describe_stacks(StackName=extension_stack(host))["Stacks"][0]
    for output in stack.get("Outputs") or []:
        if output.get("OutputKey") == ENDPOINT_OUTPUT:
            return output["OutputValue"]
    raise RuntimeError(f"{extension_stack(host)} has no {ENDPOINT_OUTPUT} output")


def install(host: str, region: str, bucket_basename: Optional[str] = None) -> None:
    """Install the extension on ``host`` from this checkout, and wait.

    ``bucket_basename`` is where the extension's artifacts are uploaded; without
    it, ``idp-feature-cli`` uses (and creates) the per-account artifacts bucket.
    """
    command = [
        "idp-feature-cli",
        "deploy",
        "--from-code",
        str(EXTENSION_DIR),
        "--host-stack-name",
        host,
        "--region",
        region,
        "--wait",
    ]
    if bucket_basename:
        command += ["--bucket-basename", bucket_basename]
    print("Installing:", " ".join(command), flush=True)
    subprocess.run(command, check=True)  # nosec B603 - fixed argv, no shell


def uninstall(cloudformation: Any, host: str) -> None:
    """Delete the extension stack, and wait. The host stack is not touched."""
    name = extension_stack(host)
    print(f"Uninstalling: deleting {name}", flush=True)
    cloudformation.delete_stack(StackName=name)
    cloudformation.get_waiter("stack_delete_complete").wait(
        StackName=name, WaiterConfig={"Delay": 20, "MaxAttempts": 90}
    )


def constants() -> Dict[str, Any]:
    from idp_common.config.models import DECISION_MODEL_MAX_INPUT_TOKENS
    from idp_common.decision_model import IMAGE_TOKENS
    from idp_common.decision_model.budget import PROMPT_RESERVE_TOKENS
    from idp_common.decision_model.wire import CONTEXT_WINDOW_TOKENS

    return {
        "CONTEXT_WINDOW_TOKENS": CONTEXT_WINDOW_TOKENS,
        "PROMPT_RESERVE_TOKENS": PROMPT_RESERVE_TOKENS,
        "IMAGE_TOKENS": IMAGE_TOKENS,
        "max_input_tokens": DECISION_MODEL_MAX_INPUT_TOKENS,
    }


def print_table(report: Report) -> None:
    print(f"\n{'case':<52} {'estimate':>8} {'measured':>8} {'secs':>6}  outcome")
    for case in report.cases:
        estimate = f"{case.estimate:.0f}" if case.estimate else "-"
        measured = "-" if case.input_tokens is None else str(case.input_tokens)
        seconds = "-" if case.seconds is None else f"{case.seconds:.1f}"
        mark = "  FAIL" if case.failures else ""
        print(
            f"{case.name:<52} {estimate:>8} {measured:>8} {seconds:>6}  "
            f"{case.outcome}{mark}"
        )
    print("\nmeasured:", json.dumps(report.measured))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--endpoint", help="decider function or alias ARN")
    target.add_argument("--stack-name", help="IDP host stack with the extension")
    parser.add_argument("--region", default=None)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument(
        "--bucket-basename",
        help="with --install: the artifacts bucket basename (region appended)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--report-dir", default=str(REPO_ROOT / "scratch" / "window-check")
    )
    args = parser.parse_args(argv)

    caller_cases = build_caller_cases()
    if args.dry_run:
        report = Report(endpoint="(dry run)", region="", constants=constants())
        report.cases = caller_cases
        print_table(report)
        limit = report.constants["max_input_tokens"]
        over = [c.name for c in caller_cases if c.estimate > limit]
        if over:
            print(f"FAIL: estimated over max_input_tokens ({limit}): {over}")
            return 1
        print(f"PASS (dry run): {len(caller_cases)} caller requests built")
        return 0

    if (args.install or args.uninstall) and not args.stack_name:
        parser.error("--install and --uninstall need --stack-name")
    if args.uninstall and not args.install:
        # Only an extension this run created is deleted, so a decider somebody
        # installed and uses is never removed by a check run against it.
        parser.error("--uninstall removes only what --install created: give both")
    if not (args.endpoint or args.stack_name):
        parser.error("give --endpoint or --stack-name (or --dry-run)")

    import boto3

    session = boto3.session.Session(region_name=args.region)
    region = session.region_name or "us-east-1"
    cloudformation = session.client("cloudformation")
    code = 2
    created = False
    try:
        if args.install:
            if extension_installed(cloudformation, args.stack_name):
                raise RuntimeError(
                    f"{extension_stack(args.stack_name)} already exists; run without "
                    "--install to check it, or delete it first"
                )
            # Set before the install, so a create that fails is still deleted.
            created = True
            install(args.stack_name, region, args.bucket_basename)
        endpoint = args.endpoint or resolve_endpoint(cloudformation, args.stack_name)
        report = Report(endpoint=endpoint, region=region, constants=constants())
        print(f"Checking {endpoint} in {region}", flush=True)
        run_check(live_sender(endpoint, region), report, caller_cases)
        print_table(report)
        out = Path(args.report_dir) / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(report.to_json(), indent=2))
        print(f"Report: {out / 'report.json'}")
        for failure in report.failures:
            print(f"FAIL: {failure}")
        code = 1 if report.failures else 0
        print("PASS" if code == 0 else f"FAIL: {len(report.failures)} finding(s)")
    except Exception as e:  # noqa: BLE001 - any setup failure is "could not run"
        print(f"Could not run: {type(e).__name__}: {e}", file=sys.stderr)
        code = 2
    finally:
        if args.uninstall and created:
            try:
                uninstall(cloudformation, args.stack_name)
            except Exception as e:  # noqa: BLE001 - reported, and fails the run
                print(
                    f"Uninstall failed: {type(e).__name__}: {e}. Delete "
                    f"{extension_stack(args.stack_name)} by hand.",
                    file=sys.stderr,
                )
                code = code or 2
    return code


if __name__ == "__main__":
    sys.exit(main())
