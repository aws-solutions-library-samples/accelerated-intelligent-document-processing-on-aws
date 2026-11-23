# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The live decision-model window check (#2051), driven against a simulated decider.

The check's verdict is only worth something if each assertion goes red when the
deployed model stops matching the constants. Each test here moves one property
of a simulated decider (its window, its own prompt, an image's cost, how its
tokenizer counts text, how it refuses) and asserts the check names it. The
simulated decider is reached through the pipeline's real client
(``DecisionModelClient``), so a refusal is classified the way the pipeline
classifies it.
"""

from __future__ import annotations

import io
import json
import subprocess
from unittest.mock import patch

import decision_model_window_check as wc
import pytest
from botocore.exceptions import ClientError

from idp_common.decision_model.budget import PROMPT_RESERVE_TOKENS
from idp_common.decision_model.wire import CONTEXT_WINDOW_TOKENS

ENDPOINT = "arn:aws:lambda:us-east-1:111122223333:function:GENAIIDP-decider:live"


class SimulatedDecider:
    """A decider Lambda that counts tokens and refuses over its window.

    Its tokenizer reads a digit or a non-ASCII character as one token and other
    text at ``chars_per_token`` characters a token, as a Qwen-family tokenizer
    roughly does; the estimate in ``idp_common.decision_model.budget`` counts
    other text at two characters a token, so it errs high against this one.

    As the deployed decider was measured to (#2051): its window holds the state
    and the LONGEST question, and its ``usage.input_tokens`` counts the state once
    and EVERY question. ``window_sums_questions`` breaks the first of those.
    """

    def __init__(
        self,
        window=CONTEXT_WINDOW_TOKENS,
        prompt=120,
        image=196,
        chars_per_token=3.5,
        report_usage=True,
        oversize_error="ValueError",
        window_sums_questions=False,
        usage_every_question=True,
    ):
        self.window = window
        self.prompt = prompt
        self.image = image
        self.chars_per_token = chars_per_token
        self.report_usage = report_usage
        self.oversize_error = oversize_error
        self.window_sums_questions = window_sums_questions
        self.usage_every_question = usage_every_question
        self.calls = 0

    def text_tokens(self, text: str) -> float:
        dense = sum(1 for c in text if c.isdigit() or ord(c) > 127)
        return dense + (len(text) - dense) / self.chars_per_token

    def tokens(self, request, every_question: bool) -> int:
        each = [
            self.text_tokens(wc._text(q.get("instructions")))
            + self.text_tokens(wc._text(q.get("criteria")))
            for q in request["questions"].values()
        ]
        state = self.text_tokens(wc._text(request["state"]))
        images = len(request.get("images") or []) * self.image
        questions = sum(each) if every_question else max(each)
        return round(self.prompt + state + images + questions)

    def invoke(self, FunctionName, InvocationType, Payload):  # noqa: N803
        self.calls += 1
        request = json.loads(Payload)
        tokens = self.tokens(request, every_question=self.window_sums_questions)
        if tokens > self.window:
            error = {
                "errorType": self.oversize_error,
                "errorMessage": f"prompt is {tokens} tokens, over {self.window}",
            }
            return {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(json.dumps(error).encode()),
            }
        body = {
            "model": "simulated",
            "answers": {
                name: {"type": "noul", "noul": 0.5}
                if q["type"] == "noul"
                else {
                    "type": "choice",
                    "choice": next(iter(q["criteria"])),
                    "probabilities": {k: 1 / len(q["criteria"]) for k in q["criteria"]},
                    "confidence": 0.0,
                }
                for name, q in request["questions"].items()
            },
        }
        if self.report_usage:
            usage = self.tokens(request, every_question=self.usage_every_question)
            body["usage"] = {"input_tokens": usage, "output_tokens": 1}
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(body).encode())}


@pytest.fixture(scope="module")
def caller_cases():
    return wc.build_caller_cases()


def _check(decider, caller_cases):
    fresh = [
        wc.Case(name=c.name, kind=c.kind, request=c.request, estimate=c.estimate)
        for c in caller_cases
    ]
    report = wc.Report(endpoint=ENDPOINT, region="us-east-1", constants={})
    with patch("idp_common.decision_model.invoker.boto3.client", return_value=decider):
        wc.run_check(wc.live_sender(ENDPOINT, "us-east-1"), report, fresh)
    return report


def test_a_decider_matching_the_constants_passes(caller_cases):
    report = _check(SimulatedDecider(), caller_cases)

    assert report.failures == []
    by_kind = {c.name: c.outcome for c in report.cases}
    assert by_kind[f"edge: {wc.EDGE_MARGIN_TOKENS} tokens under the window"] == (
        "answered"
    )
    assert by_kind[f"edge: {wc.EDGE_MARGIN_TOKENS} tokens over the window"] == (
        "refused"
    )
    callers = [c for c in report.cases if c.kind == "caller"]
    assert len(callers) == len(caller_cases)
    assert all(c.outcome == "answered" and c.input_tokens for c in callers)
    assert report.measured["image_tokens"] == {"square": 196, "portrait": 196}
    # Every request sent is in the report, in order, with its latency.
    assert report.cases[0].name == "calibration: short digits"
    assert len(report.cases) == len(caller_cases) + 7
    assert all(c.seconds is not None for c in report.cases)


def test_the_callers_build_their_largest_requests_at_the_cap(caller_cases):
    from idp_common.config.models import (
        DECISION_MODEL_MAX_INPUT_TOKENS,
        DECISION_MODEL_MAX_SECTION_PAGES,
    )

    names = [c.name for c in caller_cases]
    for caller in ("section", "confidence"):
        for mode in ("text", "both", "images"):
            assert any(n.startswith(f"{caller} {mode} ") for n in names), (caller, mode)
    for case in caller_cases:
        assert case.estimate <= DECISION_MODEL_MAX_INPUT_TOKENS, case.name
        if " text " in f" {case.name} " or " both " in f" {case.name} ":
            # Dense text is well over the window, so every text request was cut
            # to near the cap rather than sent small.
            assert case.estimate > DECISION_MODEL_MAX_INPUT_TOKENS * 0.9, case.name
        if case.name.startswith(("section both", "section images")):
            assert len(case.request["images"]) == DECISION_MODEL_MAX_SECTION_PAGES


def test_a_smaller_window_fails_the_edge_under_it(caller_cases):
    report = _check(SimulatedDecider(window=CONTEXT_WINDOW_TOKENS - 20), caller_cases)
    assert any("window is smaller than" in f for f in report.failures), report.failures


def test_a_larger_window_fails_the_edge_over_it(caller_cases):
    report = _check(SimulatedDecider(window=CONTEXT_WINDOW_TOKENS + 20), caller_cases)
    assert any("window is larger than" in f for f in report.failures), report.failures


def test_an_oversize_request_that_crashes_the_environment_fails(caller_cases):
    report = _check(SimulatedDecider(oversize_error="Runtime.ExitError"), caller_cases)
    assert any("not refused" in f for f in report.failures), report.failures


def test_a_prompt_over_the_reserve_fails(caller_cases):
    report = _check(SimulatedDecider(prompt=PROMPT_RESERVE_TOKENS + 60), caller_cases)
    assert any("PROMPT_RESERVE_TOKENS" in f for f in report.failures), report.failures


def test_an_image_over_its_estimate_fails(caller_cases):
    report = _check(SimulatedDecider(image=300), caller_cases)
    assert any("over IMAGE_TOKENS" in f for f in report.failures), report.failures


def _caller_failures(report):
    return [f for f in report.failures if f.startswith(("section ", "confidence "))]


def test_a_caller_request_refused_for_its_size_fails(caller_cases):
    # One token per character: the callers' cut requests now overflow the window.
    report = _check(SimulatedDecider(chars_per_token=1.0), caller_cases)
    refused = [f for f in _caller_failures(report) if ": refused: " in f]
    assert refused, report.failures


def test_a_tokenizer_denser_than_the_estimate_fails_even_when_answered(caller_cases):
    # A window large enough that nothing is refused, so only the estimate-errs-high
    # assertion can see that text runs 1.2 characters a token, not 2.
    report = _check(SimulatedDecider(chars_per_token=1.2, window=20_000), caller_cases)
    callers = [c for c in report.cases if c.kind == "caller"]
    assert all(c.outcome == "answered" for c in callers)
    assert any("does not err high" in f for f in _caller_failures(report)), (
        report.failures
    )


class TextOnlyDecider(SimulatedDecider):
    """A decider installed without its vision tower: it refuses any image."""

    def invoke(self, FunctionName, InvocationType, Payload):  # noqa: N803
        if json.loads(Payload).get("images"):
            error = {"errorType": "ValueError", "errorMessage": "images not enabled"}
            return {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(json.dumps(error).encode()),
            }
        return super().invoke(FunctionName, InvocationType, Payload)


def test_a_decider_without_vision_fails_every_image_request_and_finishes(
    caller_cases,
):
    report = _check(TextOnlyDecider(), caller_cases)
    assert any("the image probe was refused" in f for f in report.failures)
    refused = {c.name for c in report.cases if c.outcome == "refused"}
    with_images = {c.name for c in caller_cases if c.request.get("images")}
    assert with_images and with_images <= refused
    assert report.measured["image_tokens"] == {}


def test_a_window_that_counts_every_question_fails_the_pair_probe(caller_cases):
    report = _check(SimulatedDecider(window_sums_questions=True), caller_cases)
    assert any("longest question only" in f for f in report.failures), report.failures


class OneQuestionDecider(SimulatedDecider):
    """A decider that refuses a request with more than one question."""

    def invoke(self, FunctionName, InvocationType, Payload):  # noqa: N803
        if len(json.loads(Payload)["questions"]) > 1:
            error = {"errorType": "ValueError", "errorMessage": "one question only"}
            return {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(json.dumps(error).encode()),
            }
        return super().invoke(FunctionName, InvocationType, Payload)


def test_a_second_question_that_cannot_be_measured_fails(caller_cases):
    report = _check(OneQuestionDecider(), caller_cases)
    assert any(
        "a second question was refused" in f and "cannot be probed" in f
        for f in report.failures
    ), report.failures


def test_each_confidence_request_carries_one_question(caller_cases):
    confidence = [c for c in caller_cases if c.name.startswith("confidence ")]
    assert len(confidence) == 2 * len(wc.DENSE_STYLES) * 2 + 2
    assert all(len(c.request["questions"]) == 1 for c in confidence)


def test_a_caller_request_with_two_questions_is_not_compared(caller_cases):
    case = caller_cases[0]
    two = dict(case.request)
    (question,) = two["questions"].values()
    two["questions"] = {"a": question, "b": question}
    pair = wc.Case(name="section two questions", kind="caller", request=two)
    pair.estimate = wc.estimated_tokens(two)
    report = _check(SimulatedDecider(), [pair])
    assert report.failures == [
        "section two questions: carries more than one question, and "
        "usage.input_tokens counts every one, so it cannot be compared with the "
        "window"
    ]


def test_a_decider_that_reports_no_usage_stops_the_check(caller_cases):
    decider = SimulatedDecider(report_usage=False)
    report = _check(decider, caller_cases)
    assert len(report.failures) == 1
    assert "nothing else can be measured" in report.failures[0]
    assert decider.calls == 1


def test_the_dry_run_builds_every_request_and_touches_nothing(capsys):
    with patch("boto3.session.Session", side_effect=AssertionError("AWS reached")):
        assert wc.main(["--dry-run"]) == 0
    assert "PASS (dry run)" in capsys.readouterr().out


def test_install_and_uninstall_need_a_host_stack():
    with pytest.raises(SystemExit):
        wc.main(["--endpoint", ENDPOINT, "--install"])


def test_the_dry_run_fails_a_request_estimated_over_the_cap(capsys):
    oversize = wc.Case(name="oversize", kind="caller", request={}, estimate=99_999)
    with patch.object(wc, "build_caller_cases", return_value=[oversize]):
        assert wc.main(["--dry-run"]) == 1
    assert "estimated over max_input_tokens" in capsys.readouterr().out


class FakeCloudFormation:
    """The extension stack, absent until an install creates it."""

    def __init__(self, outputs, exists=False, delete_error=None):
        self.outputs = outputs
        self.exists = exists
        self.delete_error = delete_error
        self.deleted = []
        self.waited = []

    def describe_stacks(self, StackName):  # noqa: N803
        if not self.exists:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationError",
                        "Message": f"Stack with id {StackName} does not exist",
                    }
                },
                "DescribeStacks",
            )
        return {"Stacks": [{"StackName": StackName, "Outputs": self.outputs}]}

    def delete_stack(self, StackName):  # noqa: N803
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(StackName)

    def get_waiter(self, name):
        fake = self

        class Waiter:
            def wait(self, StackName, WaiterConfig):  # noqa: N803
                fake.waited.append((name, StackName))

        return Waiter()


OUTPUTS = [{"OutputKey": "DeciderEndpoint", "OutputValue": ENDPOINT}]
STACK = "host1-feature-decision-model-decider"


def _main(argv, decider, cloudformation, tmp_path, caller_cases, install_error=None):
    session = type(
        "S",
        (),
        {"region_name": "us-east-1", "client": lambda self, name: cloudformation},
    )()
    runs = []

    def run(cmd, check):
        runs.append(cmd)
        cloudformation.exists = True
        if install_error:
            raise install_error

    with (
        patch("boto3.session.Session", return_value=session),
        patch("idp_common.decision_model.invoker.boto3.client", return_value=decider),
        patch.object(wc, "build_caller_cases", return_value=caller_cases),
        patch.object(wc.subprocess, "run", side_effect=run),
    ):
        code = wc.main([*argv, "--report-dir", str(tmp_path)])
    return code, runs


def test_main_installs_checks_reports_and_uninstalls(tmp_path, capsys, caller_cases):
    cfn = FakeCloudFormation(OUTPUTS)
    argv = ["--stack-name", "host1", "--install", "--uninstall"]
    code, runs = _main(
        [*argv, "--bucket-basename", "my-bucket"],
        SimulatedDecider(),
        cfn,
        tmp_path,
        caller_cases[:2],
    )

    assert code == 0
    (install,) = runs
    assert install[:4] == [
        "idp-feature-cli",
        "deploy",
        "--from-code",
        str(wc.EXTENSION_DIR),
    ]
    assert install[install.index("--host-stack-name") + 1] == "host1"
    assert install[install.index("--bucket-basename") + 1] == "my-bucket"
    assert cfn.deleted == [STACK]
    assert cfn.waited == [("stack_delete_complete", STACK)]
    (report_file,) = tmp_path.glob("*/report.json")
    report = json.loads(report_file.read_text())
    assert report["endpoint"] == ENDPOINT and report["failures"] == []
    assert [c["name"] for c in report["cases"]][-2:] == [
        c.name for c in caller_cases[:2]
    ]
    out = capsys.readouterr().out
    assert "Installing:" in out and "PASS" in out
    assert caller_cases[0].name in out


def test_main_fails_names_each_failure_and_still_uninstalls(
    tmp_path, capsys, caller_cases
):
    cfn = FakeCloudFormation(OUTPUTS)
    code, _ = _main(
        ["--stack-name", "host1", "--install", "--uninstall"],
        SimulatedDecider(image=300),
        cfn,
        tmp_path,
        caller_cases[:1],
    )
    assert code == 1
    assert cfn.deleted == [STACK]
    assert "FAIL: image cost: one square page: " in capsys.readouterr().out


def test_main_checks_an_installed_extension_and_leaves_it(tmp_path, caller_cases):
    cfn = FakeCloudFormation(OUTPUTS, exists=True)
    code, runs = _main(
        ["--stack-name", "host1"], SimulatedDecider(), cfn, tmp_path, caller_cases[:1]
    )
    assert code == 0 and runs == [] and cfn.deleted == []


def test_install_refuses_a_host_that_already_has_the_extension(
    tmp_path, capsys, caller_cases
):
    cfn = FakeCloudFormation(OUTPUTS, exists=True)
    code, runs = _main(
        ["--stack-name", "host1", "--install", "--uninstall"],
        SimulatedDecider(),
        cfn,
        tmp_path,
        caller_cases[:1],
    )
    assert code == 2
    assert runs == []
    assert cfn.deleted == [], "an extension this run did not create was deleted"
    assert "already exists" in capsys.readouterr().err


def test_a_failed_install_is_still_uninstalled(tmp_path, caller_cases):
    cfn = FakeCloudFormation(OUTPUTS)
    error = subprocess.CalledProcessError(1, ["idp-feature-cli"])
    code, runs = _main(
        ["--stack-name", "host1", "--install", "--uninstall"],
        SimulatedDecider(),
        cfn,
        tmp_path,
        caller_cases[:1],
        install_error=error,
    )
    assert code == 2 and len(runs) == 1
    assert cfn.deleted == [STACK]


def test_a_failed_uninstall_turns_a_pass_into_could_not_run(
    tmp_path, capsys, caller_cases
):
    cfn = FakeCloudFormation(OUTPUTS, delete_error=RuntimeError("delete rejected"))
    code, _ = _main(
        ["--stack-name", "host1", "--install", "--uninstall"],
        SimulatedDecider(),
        cfn,
        tmp_path,
        caller_cases[:1],
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "Uninstall failed: RuntimeError: delete rejected" in err
    assert STACK in err


def test_uninstall_needs_install():
    with pytest.raises(SystemExit):
        wc.main(["--stack-name", "host1", "--uninstall"])


def test_main_without_the_endpoint_output_could_not_run(tmp_path, caller_cases):
    cfn = FakeCloudFormation([], exists=True)
    code, _ = _main(["--stack-name", "host1"], SimulatedDecider(), cfn, tmp_path, [])
    assert code == 2
    assert cfn.deleted == []


def _boundary_sender(caller_excess: int):
    """Answers every probe as the measured decider did, and each caller request at
    exactly its estimate plus PROMPT_RESERVE_TOKENS plus ``caller_excess``."""

    def send(case):
        case.seconds = 0.0
        if case.kind == "edge-over":
            case.outcome = "refused"
            return
        case.outcome = "answered"
        if case.kind == "caller":
            case.input_tokens = (
                int(case.estimate) + PROMPT_RESERVE_TOKENS + caller_excess
            )
        else:
            case.input_tokens = (
                277
                + 68 * (len(case.request["questions"]) - 1)
                + (199 * len(case.request.get("images") or []))
            )

    return send


@pytest.mark.parametrize("excess, fails", [(0, False), (1, True)])
def test_the_estimate_assertion_sits_at_the_reserve(caller_cases, excess, fails):
    report = wc.Report(endpoint=ENDPOINT, region="us-east-1", constants={})
    wc.run_check(_boundary_sender(excess), report, caller_cases[:1])
    assert bool(_caller_failures(report)) is fails, report.failures


def test_usage_that_ignores_a_second_question_fails(caller_cases):
    report = _check(SimulatedDecider(usage_every_question=False), caller_cases)
    assert any(
        "added 0 tokens" in f and "cannot be probed" in f for f in report.failures
    ), report.failures


def _png_size(b64: str):
    import base64

    from PIL import Image

    return Image.open(io.BytesIO(base64.b64decode(b64))).size


def test_what_the_check_sends_is_pinned(caller_cases):
    from idp_common.decision_model.wire import IMAGE_MAX_SIDE

    # The two densest shapes measured against the pinned tokenizer, and figures.
    assert {"figures", "table", "codes"} <= set(wc.DENSE_STYLES)
    for style in wc.DENSE_STYLES:
        assert any(f" {style} " in c.name for c in caller_cases), style
    # Eight classes with full descriptions make the section question realistic.
    assert len(wc.SECTION_CLASSES) == 8
    assert all(len(d) > 150 for d in wc.SECTION_CLASSES.values())
    section = next(c for c in caller_cases if c.name.startswith("section text"))
    criteria = section.request["questions"]["doc_class"]["criteria"]
    assert criteria == wc.SECTION_CLASSES
    # Every page image is at the side the decider reads images at.
    sizes = {_png_size(i) for c in caller_cases for i in c.request.get("images", [])}
    assert sizes == {(IMAGE_MAX_SIDE, IMAGE_MAX_SIDE)}


def test_the_letter_only_and_rare_cjk_shapes_are_sent_and_cut(caller_cases):
    """The shapes #2212 weighs: each page is over the window, so every text
    request carrying it is cut, and each is the shape its name says."""
    from idp_common.decision_model import estimate_tokens
    from idp_common.decision_model.wire import CONTEXT_WINDOW_TOKENS

    shapes = ("mixedcase", "base64", "capscodes", "rarehanzi", "rarenames")
    assert set(shapes) <= set(wc.DENSE_STYLES)
    for style in shapes:
        page = wc.dense_page(style, 1)
        assert estimate_tokens(page) > CONTEXT_WINDOW_TOKENS, style
        for case in caller_cases:
            if f" {style} " in case.name and " text " in case.name:
                assert 0 < len(wc._text(case.request["state"])) < len(page), case.name
    letters = wc.dense_page("mixedcase", 1) + wc.dense_page("base64", 1)
    assert not any(c.isdigit() for c in letters)
    assert any(c.islower() for c in letters) and any(c.isupper() for c in letters)
    capitals = wc.dense_page("capscodes", 1).replace("\n", " ").replace(" ", "")
    assert capitals.isalpha() and capitals.isupper()
    # Every character of rarehanzi is in GB 2312's second level (rows 0xD8-0xF7).
    rare = wc.dense_page("rarehanzi", 1).replace("\n", "")
    assert all(0xD8 <= c.encode("gb2312")[0] <= 0xF7 for c in rare)


def test_the_probes_send_full_size_page_images(caller_cases):
    from idp_common.decision_model.wire import IMAGE_MAX_SIDE

    report = _check(SimulatedDecider(), caller_cases[:1])
    probes = {c.name: c for c in report.cases if c.name.startswith("image cost")}
    square, portrait = (
        probes["image cost: one square page"],
        probes["image cost: one portrait page"],
    )
    assert [_png_size(i) for i in square.request["images"]] == [
        (IMAGE_MAX_SIDE, IMAGE_MAX_SIDE)
    ]
    # A letter page at 150 dpi, scaled as the pipeline's encode_image scales it.
    assert [_png_size(i) for i in portrait.request["images"]] == [(346, IMAGE_MAX_SIDE)]


class UnreadableCloudFormation(FakeCloudFormation):
    """A host whose extension stack cannot be read: throttled, or access denied."""

    def __init__(self, code):
        super().__init__(OUTPUTS, exists=True)
        self.code = code

    def describe_stacks(self, StackName):  # noqa: N803
        raise ClientError({"Error": {"Code": self.code, "Message": "no"}}, "Describe")


@pytest.mark.parametrize("code", ["Throttling", "AccessDenied"])
def test_an_unreadable_extension_stack_is_neither_installed_over_nor_deleted(
    tmp_path, caller_cases, code
):
    cfn = UnreadableCloudFormation(code)
    rc, runs = _main(
        ["--stack-name", "host1", "--install", "--uninstall"],
        SimulatedDecider(),
        cfn,
        tmp_path,
        caller_cases[:1],
    )
    assert rc == 2
    assert runs == [] and cfn.deleted == []
