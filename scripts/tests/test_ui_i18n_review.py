# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pin what `scripts/ui_i18n_review.py` sends a model, what it believes of the answer,
what it costs, and what it writes.

The script behind `make ui-i18n-review` calls Amazon Bedrock, so every test here hands
it a fake client, or the real one with made-up credentials and its HTTP send replaced:
nothing needs credentials, a Region or the network, and the fake records every request
so the tests can say exactly what was sent. The coverage worker
is replaced by canned answers where a test is about the script's own logic; the
end-to-end test runs the real worker over a catalog in a temporary directory when the
UI's dependencies are installed, and in CI they always are, so there it never skips.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, ProfileNotFound

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Same rule as ``IN_CI`` in ``test_ui_i18n_coverage.py``.
IN_CI = bool(os.getenv("CI") or os.getenv("GITLAB_CI") or os.getenv("GITHUB_ACTIONS"))


def _load_module():
    """Load `scripts/ui_i18n_review.py` by path (scripts/ is not a package)."""
    path = REPO_ROOT / "scripts" / "ui_i18n_review.py"
    spec = importlib.util.spec_from_file_location("_ui_i18n_review", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()

NBSP = "\u00a0"


def source_hash(text: str) -> str:
    """The catalog's ``sourceHash``, restated so the end-to-end catalog is well formed."""
    digest = hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8"))
    return digest.hexdigest()[:16]


#: The default model's rates, as config_library/pricing.yaml has them.
PRICING = """pricing:
  - name: bedrock/us.anthropic.claude-opus-5-5
    units:
      - name: inputTokens
        price: "4.0E-6"
      - name: outputTokens
        price: "2.0E-5"
      - name: cacheReadInputTokens
        price: "2.0E-7"
      - name: cacheWriteInputTokens
        price: "5.0E-6"
"""

EN = {
    "nav.count": {
        "defaultMessage": "{count, plural, one {# document} other {# documents}}",
        "description": "Count of documents in the list",
    },
    "nav.greeting": {"defaultMessage": "Hello {name}", "description": "Greeting"},
    "nav.queue": {
        "defaultMessage": "Open the Review Queue",
        "description": "Link to the review queue",
    },
    "nav.upload": {
        "defaultMessage": "Upload documents",
        "description": "Button that opens the upload page",
    },
}

FR_TEXT = {
    "nav.count": "{count,plural,one{# document} other{# documents}}",
    "nav.greeting": "Bonjour {name}",
    "nav.queue": "Ouvrir la file d’attente de révision",
    "nav.upload": "Téléverser des documents",
}

GLOSSARY = (
    "en,fr-CA,note\n"
    "document,document,Noun.\n"
    "review queue,file d’attente de révision,Noun phrase.\n"
)


def _fr(texts: dict[str, str] = FR_TEXT, en=EN) -> dict[str, Any]:
    return {
        entry_id: {
            "message": text,
            "sourceHash": source_hash(en[entry_id]["defaultMessage"]),
        }
        for entry_id, text in texts.items()
    }


def _coverage(locale="fr-CA", invalid=(), stale=(), problems=()) -> str:
    return json.dumps(
        {
            "sourceKeys": 4,
            "locales": {
                locale: {
                    "translated": 4,
                    "missing": [],
                    "stale": list(stale),
                    "invalid": list(invalid),
                    "problems": list(problems),
                    "dead": [],
                    "coveragePercent": 100,
                }
            },
        }
    )


def _tree(
    tmp_path: Path,
    monkeypatch,
    *,
    en=EN,
    fr=None,
    glossary: str | None = GLOSSARY,
    coverage: str | None = None,
):
    """A checkout whose coverage worker gives a canned answer; returns it and the worker calls."""
    layout = MODULE.Layout(tmp_path)
    layout.catalog_dir.mkdir(parents=True)
    layout.parser_package.mkdir(parents=True)
    layout.pricing.parent.mkdir(parents=True)
    layout.pricing.write_text(PRICING, encoding="utf-8")
    layout.catalog("en").write_text(json.dumps(en), encoding="utf-8")
    layout.catalog("fr-CA").write_text(json.dumps(fr or _fr(en=en)), encoding="utf-8")
    if glossary is not None:
        layout.glossary("fr-CA").write_text(glossary, encoding="utf-8")
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command[2:])
        return subprocess.CompletedProcess(
            command, 0, stdout=coverage or _coverage(), stderr=""
        )

    monkeypatch.setattr(MODULE, "run", run)
    return layout, calls


# --- a fake Bedrock ----------------------------------------------------------------

USAGE = {
    "inputTokens": 1_000,
    "outputTokens": 500,
    "totalTokens": 4_500,
    "cacheReadInputTokens": 0,
    "cacheWriteInputTokens": 3_000,
}


def reply(
    reviews,
    *,
    usage: Any = USAGE,
    stop="tool_use",
    text: str | None = None,
    retries: int | None = 0,
):
    """A Converse response: thinking, optional prose, and the review tool's call, with the
    ``RetryAttempts`` botocore adds to every response (None leaves it out)."""
    content: list[dict[str, Any]] = [
        {"reasoningContent": {"reasoningText": {"text": "Looking at each message."}}}
    ]
    if text is not None:
        content.append({"text": text})
    if reviews is not None:
        content.append(
            {
                "toolUse": {
                    "toolUseId": "tooluse-1",
                    "name": "record_review",
                    "input": {"reviews": reviews},
                }
            }
        )
    response: dict[str, Any] = {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop,
    }
    if usage is not None:
        response["usage"] = usage
    if retries is not None:
        response["ResponseMetadata"] = {"HTTPStatusCode": 200, "RetryAttempts": retries}
    return response


def sent(request) -> list[dict[str, Any]]:
    """The entries one request carried."""
    text = request["messages"][0]["content"][0]["text"]
    return json.loads(text.split("\n", 1)[1])


def batch_ceiling(request) -> float:
    """The most the batch one request carried can cost, every attempt included."""
    return MODULE.ceiling_usd(
        MODULE.Rates(4e-6, 2e-5, 2e-7, 5e-6),
        request["system"][0]["text"],
        [
            MODULE.Entry(
                id=i["id"],
                english=i["english"],
                description=i["description"],
                translation=i["translation"],
                terms=i.get("glossaryTerms", []),
            )
            for i in sent(request)
        ],
    )


def review(item, problems=(), reason="ok", rendering=None) -> dict[str, Any]:
    return {
        "id": item["id"],
        "problems": list(problems),
        "reason": reason,
        "rendering": item["translation"] if rendering is None else rendering,
    }


def agree(request):
    """Every entry acceptable, worded as it is."""
    return reply([review(item) for item in sent(request)])


class FakeBedrock:
    """Stands in for a boto3 bedrock-runtime client: records requests, answers through ``answer``."""

    def __init__(self, answer=agree):
        self.answer = answer
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request):
        self.requests.append(request)
        return self.answer(request)


def _use_client(monkeypatch, client) -> list[tuple[str, str | None]]:
    asked: list[tuple[str, str | None]] = []

    def make_client(region, profile):
        asked.append((region, profile))
        return client

    monkeypatch.setattr(MODULE, "make_client", make_client)
    return asked


def _no_client(monkeypatch) -> None:
    def make_client(region, profile):
        raise AssertionError("no AWS client may be built on this path")

    monkeypatch.setattr(MODULE, "make_client", make_client)


RUN = ["--locale", "fr-CA", "--region", "ca-central-1"]


def _run(layout, tmp_path, *extra: str) -> int:
    return MODULE.main([*RUN, "--out", str(tmp_path / "out"), *extra], layout=layout)


def _report(tmp_path) -> dict[str, Any]:
    return json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))


def _outcomes(record) -> dict[str, str]:
    return {entry["id"]: entry["outcome"] for entry in record["entries"]}


def _many(count: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """``count`` short entries, for batching and the cap."""
    en = {
        f"m.{n:03}": {"defaultMessage": f"Message {n}", "description": "Test"}
        for n in range(count)
    }
    fr = {
        entry_id: {"message": f"Message {n}", "sourceHash": source_hash(f"Message {n}")}
        for n, entry_id in enumerate(en)
    }
    return en, fr


# --- the plan and the estimate ------------------------------------------------------


def test_a_dry_run_prints_the_plan_and_the_estimate_and_calls_nothing(
    tmp_path, monkeypatch, capsys
):
    layout, calls = _tree(tmp_path, monkeypatch)
    _no_client(monkeypatch)

    assert MODULE.main(["--locale", "fr-CA", "--dry-run"], layout=layout) == 0

    out = capsys.readouterr().out
    assert (
        "fr-CA: 4 entries to review in 1 batch with us.anthropic.claude-opus-5-5; about US$"
        in out
    )
    assert "(cap US$20.00)" in out
    assert calls == [[str(layout.catalog_dir), "--json", "--locale", "fr-CA"]]
    assert not layout.reports_dir.exists()


def test_an_estimate_over_the_cap_refuses_to_start(tmp_path, monkeypatch, capsys):
    layout, _ = _tree(tmp_path, monkeypatch)
    _no_client(monkeypatch)

    assert _run(layout, tmp_path, "--max-usd", "0.001") == 2

    assert "is over --max-usd 0.00" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_a_first_batch_that_could_pass_the_cap_is_never_sent(
    tmp_path, monkeypatch, capsys
):
    layout, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    # The estimate is cents, but one batch at its largest (every retry, the output
    # limit) costs about a dollar.
    assert _run(layout, tmp_path, "--max-usd", "0.5") == 2

    assert "the first batch could cost up to US$" in capsys.readouterr().err
    assert client.requests == []
    assert not (tmp_path / "out").exists()


def test_limit_takes_the_first_entries_in_id_order(tmp_path, monkeypatch, capsys):
    layout, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, tmp_path, "--limit", "2") == 0

    assert [item["id"] for item in sent(client.requests[0])] == [
        "nav.count",
        "nav.greeting",
    ]
    assert "2 more left for a later run by --limit" in capsys.readouterr().out


def test_batches_hold_at_most_40_entries_and_12000_characters():
    def entry(n: int, size: int):
        return MODULE.Entry(
            id=f"m.{n}", english="x" * size, description="", translation="", terms=[]
        )

    small = [entry(n, 10) for n in range(85)]
    assert [len(batch) for batch in MODULE.make_batches(small)] == [40, 40, 5]

    large = [entry(n, 5_000) for n in range(5)]
    assert [len(batch) for batch in MODULE.make_batches(large)] == [2, 2, 1]

    alone = [entry(0, 20_000), entry(1, 10)]
    assert [len(batch) for batch in MODULE.make_batches(alone)] == [1, 1]


# --- the request ---------------------------------------------------------------------


def test_the_request_is_shaped_for_claude_opus_5_5(tmp_path, monkeypatch):
    layout, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock()
    asked = _use_client(monkeypatch, client)

    assert _run(layout, tmp_path, "--profile", "dev") == 0

    assert asked == [("ca-central-1", "dev")]
    (request,) = client.requests
    assert request["modelId"] == "us.anthropic.claude-opus-5-5"
    assert request["system"][1] == {"cachePoint": {"type": "default"}}
    assert request["toolConfig"]["toolChoice"] == {"auto": {}}
    (tool,) = request["toolConfig"]["tools"]
    assert tool["toolSpec"]["name"] == "record_review"
    item = tool["toolSpec"]["inputSchema"]["json"]["properties"]["reviews"]["items"]
    assert item["properties"]["problems"]["items"]["enum"] == list(MODULE.PROBLEMS)
    # Claude Opus 5.5 rejects a temperature and a forced tool; its thinking is on.
    assert request["inferenceConfig"] == {"maxTokens": MODULE.MAX_OUTPUT_TOKENS}
    assert request["additionalModelRequestFields"] == {
        "output_config": {"effort": "low"}
    }


def test_the_prefix_holds_the_rules_and_the_whole_glossary(tmp_path, monkeypatch):
    layout, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, tmp_path) == 0

    prefix = client.requests[0]["system"][0]["text"]
    assert "The formal register: vous, never tu." in prefix
    # A verb, so not in the glossary, which Amazon Translate applies in every context.
    assert (
        "- Assign is attribuer, with its forms attribué (agreeing in gender and number), "
        "attribution, réattribuer, désattribuer and non attribué; never assigner, "
        "affecter or assignation.\n"
    ) in prefix
    assert "never list a problem about them" in prefix
    assert (
        "en,fr-CA,note\ndocument,document,Noun.\nreview queue,file d’attente de révision,Noun phrase.\n"
        in prefix
    )
    for name, meaning in MODULE.PROBLEMS.items():
        assert f"- {name}: {meaning}" in prefix


def test_each_entry_carries_its_description_translation_and_glossary_terms(
    tmp_path, monkeypatch
):
    layout, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, tmp_path) == 0

    by_id = {item["id"]: item for item in sent(client.requests[0])}
    assert by_id["nav.queue"] == {
        "id": "nav.queue",
        "english": "Open the Review Queue",
        "description": "Link to the review queue",
        "translation": "Ouvrir la file d’attente de révision",
        "glossaryTerms": ["review queue"],
    }
    assert by_id["nav.count"]["glossaryTerms"] == ["document"]
    assert "glossaryTerms" not in by_id["nav.greeting"]


def test_every_request_opens_with_the_same_cached_prefix(tmp_path, monkeypatch):
    en, fr = _many(45)
    layout, _ = _tree(tmp_path, monkeypatch, en=en, fr=fr)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, tmp_path) == 0

    assert len(client.requests) == 2
    assert client.requests[0]["system"] == client.requests[1]["system"]
    assert [len(sent(request)) for request in client.requests] == [40, 5]


def test_glossary_terms_match_whole_words_in_any_case():
    glossary = MODULE.Glossary(
        rows=(
            ("queue", "file d’attente", ""),
            ("review queue", "file", ""),
            ("set", "jeu", ""),
        ),
        source=Path("glossary.fr-CA.csv"),
        sha256="0" * 64,
    )
    assert glossary.terms_in("The Review Queue is empty") == ["queue", "review queue"]
    assert glossary.terms_in("Queues and settings") == []


# --- the answer ------------------------------------------------------------------------


def test_the_agreement_classes(tmp_path, monkeypatch):
    def answer(request):
        items = {item["id"]: item for item in sent(request)}
        return reply(
            [
                # The same text but for the spacing and the apostrophe: the same.
                review(
                    items["nav.queue"],
                    rendering="Ouvrir la file d'attente  de révision",
                ),
                # Worded otherwise, no problem: the current one stands.
                review(items["nav.upload"], rendering="Téléversez des documents"),
                # A problem: a person decides.
                review(
                    items["nav.greeting"],
                    problems=["register"],
                    reason="Too familiar for this UI.",
                    rendering="Bonjour, {name}",
                ),
                review(items["nav.count"]),
            ]
        )

    layout, _ = _tree(tmp_path, monkeypatch)
    _use_client(monkeypatch, FakeBedrock(answer))

    assert _run(layout, tmp_path) == 0

    record = _report(tmp_path)
    assert _outcomes(record) == {
        "nav.count": "same",
        "nav.greeting": "disagreement",
        "nav.queue": "same",
        "nav.upload": "same-meaning",
    }
    assert record["counts"] == {
        "same": 2,
        "same-meaning": 1,
        "disagreement": 1,
        "unreviewed": 0,
        "skipped": 0,
    }
    assert record["agreementPercent"] == 75.0
    assert record["reviewed"] == 4
    markdown = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert (
        "| `nav.greeting` | Hello {name} | Bonjour {name} | Bonjour, {name} | register | "
        "Too familiar for this UI. |"
    ) in markdown


def test_the_comparison_ignores_spacing_and_apostrophe_style_only():
    assert MODULE.comparable(f"Statut{NBSP}: «{NBSP}prêt{NBSP}» l'état") == (
        MODULE.comparable("Statut : « prêt » l’état")
    )
    assert MODULE.comparable("Statut : prêt") != MODULE.comparable("statut : prêt")


def test_invalid_and_stale_entries_are_listed_and_never_sent(tmp_path, monkeypatch):
    coverage = _coverage(
        invalid=["nav.greeting"],
        stale=["nav.upload"],
        problems=[
            "fr-CA.json: nav.greeting: the translation is missing argument(s) {name}"
        ],
    )
    layout, _ = _tree(tmp_path, monkeypatch, coverage=coverage)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, tmp_path) == 0

    assert [item["id"] for item in sent(client.requests[0])] == [
        "nav.count",
        "nav.queue",
    ]
    record = _report(tmp_path)
    assert record["invalid"] == ["nav.greeting"]
    assert record["stale"] == ["nav.upload"]
    assert record["translated"] == 2
    markdown = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "- Invalid, the build rejects them: 1" in markdown
    assert "- Stale, translate them again first: 1" in markdown


def _answer_with(change):
    """A model that agrees, except for what ``change`` does to its list of reviews."""

    def answer(request):
        return reply(change([review(item) for item in sent(request)]))

    return answer


def _set(entry_id: str, **fields):
    def change(reviews):
        for item in reviews:
            if item["id"] == entry_id:
                item.update(fields)
        return reviews

    return change


@pytest.mark.parametrize(
    "answer, reason",
    [
        (
            lambda request: reply(None, text="Here is my review."),
            "the model answered without the record_review tool (stop reason 'tool_use')",
        ),
        (
            lambda request: reply([], stop="max_tokens"),
            "the answer was cut off at the token limit",
        ),
        (
            _answer_with(_set("nav.queue", problems=["style"])),
            "it names problems outside the list: style",
        ),
        (
            _answer_with(_set("nav.queue", problems=["meaning", "meaning"])),
            "it names a problem twice",
        ),
        (
            _answer_with(_set("nav.queue", problems="meaning")),
            "its problems are not a list of names",
        ),
        (_answer_with(_set("nav.queue", reason=None)), "its reason is not text"),
        (
            _answer_with(_set("nav.queue", rendering=" ")),
            "it has no translation of its own",
        ),
        (
            _answer_with(_set("nav.queue", rendering="x" * 4_001)),
            "its translation is longer than 4000 characters",
        ),
        (
            _answer_with(lambda reviews: reviews + [dict(reviews[2])]),
            "the model answered for it twice",
        ),
        (
            _answer_with(
                lambda reviews: [r for r in reviews if r["id"] != "nav.queue"]
            ),
            "the model gave no answer for it",
        ),
    ],
    ids=[
        "prose",
        "cut off",
        "unknown problem",
        "repeated problem",
        "problems not a list",
        "reason not text",
        "empty translation",
        "long translation",
        "answered twice",
        "no answer",
    ],
)
def test_an_unusable_answer_is_listed_and_never_counted(
    tmp_path, monkeypatch, capsys, answer, reason
):
    layout, _ = _tree(tmp_path, monkeypatch)
    _use_client(monkeypatch, FakeBedrock(answer))

    assert _run(layout, tmp_path) == 1

    record = _report(tmp_path)
    entry = next(e for e in record["entries"] if e["id"] == "nav.queue")
    assert entry["outcome"] == "unreviewed"
    assert reason in entry["reason"]
    assert record["counts"]["unreviewed"] >= 1
    assert "unreviewed (see the report)" in capsys.readouterr().out


def test_an_answer_for_an_id_of_no_batch_counts_for_nothing(tmp_path, monkeypatch):
    def answer(request):
        reviews = [review(item) for item in sent(request)]
        return reply(
            reviews
            + [{"id": "nav.other", "problems": [], "reason": "ok", "rendering": "x"}]
        )

    layout, _ = _tree(tmp_path, monkeypatch)
    _use_client(monkeypatch, FakeBedrock(answer))

    assert _run(layout, tmp_path) == 0
    assert set(_outcomes(_report(tmp_path))) == set(EN)


def test_a_long_reason_is_cut(tmp_path, monkeypatch):
    layout, _ = _tree(tmp_path, monkeypatch)
    answer = _answer_with(_set("nav.queue", problems=["meaning"], reason="y" * 900))
    _use_client(monkeypatch, FakeBedrock(answer))

    assert _run(layout, tmp_path) == 0
    entry = next(e for e in _report(tmp_path)["entries"] if e["id"] == "nav.queue")
    assert entry["reason"] == "y" * MODULE.MAX_REASON_CHARS


# --- the cost --------------------------------------------------------------------------


def test_the_cost_comes_from_the_reported_usage_at_the_model_rates(
    tmp_path, monkeypatch
):
    layout, _ = _tree(tmp_path, monkeypatch)
    _use_client(monkeypatch, FakeBedrock())

    assert _run(layout, tmp_path) == 0

    cost = _report(tmp_path)["cost"]
    assert cost == {
        "usd": round(1_000 * 4e-6 + 500 * 2e-5 + 3_000 * 5e-6, 4),
        "maxUsd": 20.0,
        "calls": 1,
        "retriedUsd": 0.0,
        "retriedAttempts": 0,
        "inputTokens": 1_000,
        "outputTokens": 500,
        "cacheReadInputTokens": 0,
        "cacheWriteInputTokens": 3_000,
    }


@pytest.mark.parametrize("retries", [0, 2])
def test_each_attempt_botocore_retried_is_counted_at_its_most_on_a_line_of_its_own(
    tmp_path, monkeypatch, capsys, retries
):
    layout, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock(
        lambda request: reply([review(i) for i in sent(request)], retries=retries)
    )
    _use_client(monkeypatch, client)

    assert _run(layout, tmp_path) == 0

    retried = retries * batch_ceiling(client.requests[0]) / MODULE.ATTEMPTS
    cost = _report(tmp_path)["cost"]
    assert cost["usd"] == round(1_000 * 4e-6 + 500 * 2e-5 + 3_000 * 5e-6 + retried, 4)
    assert cost["retriedUsd"] == round(retried, 4)
    assert cost["retriedAttempts"] == retries
    out = capsys.readouterr().out
    markdown = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    note = (
        f"US${retried:.2f} of the cost counts {retries} retried or failed attempts at "
        "their most: a throttled attempt is not billed"
    )
    if retries:
        assert f"\n  {note}\n" in out
        assert f"\n\n{note}.\n" in markdown
    else:
        assert "retried" not in out
        assert "retried" not in markdown


def test_a_response_that_does_not_say_how_often_it_was_retried_counts_every_retry(
    tmp_path, monkeypatch
):
    layout, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock(
        lambda request: reply([review(i) for i in sent(request)], retries=None)
    )
    _use_client(monkeypatch, client)

    assert _run(layout, tmp_path) == 0

    per_attempt = batch_ceiling(client.requests[0]) / MODULE.ATTEMPTS
    cost = _report(tmp_path)["cost"]
    assert cost["retriedAttempts"] == MODULE.ATTEMPTS - 1
    assert cost["retriedUsd"] == round((MODULE.ATTEMPTS - 1) * per_attempt, 4)


def test_the_cap_stops_the_run_before_a_batch_that_could_pass_it(
    tmp_path, monkeypatch, capsys
):
    en, fr = _many(41)
    layout, _ = _tree(tmp_path, monkeypatch, en=en, fr=fr)
    # The first batch reports US$4 of output; the second could cost about a dollar more.
    dear = {
        **USAGE,
        "outputTokens": 200_000,
        "inputTokens": 0,
        "cacheWriteInputTokens": 0,
    }
    client = FakeBedrock(
        lambda request: reply([review(i) for i in sent(request)], usage=dear)
    )
    _use_client(monkeypatch, client)

    assert _run(layout, tmp_path, "--max-usd", "4.5") == 1

    assert len(client.requests) == 1
    record = _report(tmp_path)
    assert record["counts"]["skipped"] == 1
    assert record["cost"]["usd"] <= 4.5
    assert "the next batch could take the run past --max-usd 4.50" in record["stopped"]
    assert "the run stopped early" in capsys.readouterr().out


def test_an_unreported_usage_is_counted_at_its_most(tmp_path, monkeypatch):
    layout, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock(
        lambda request: reply([review(i) for i in sent(request)], usage=None)
    )
    _use_client(monkeypatch, client)

    assert _run(layout, tmp_path) == 0

    cost = _report(tmp_path)["cost"]
    # The ceiling counts every attempt already, so the retries add nothing on top.
    assert cost["usd"] == round(batch_ceiling(client.requests[0]), 4)
    assert cost["retriedUsd"] == 0


def test_a_model_with_no_price_is_refused(tmp_path, monkeypatch, capsys):
    layout, _ = _tree(tmp_path, monkeypatch)
    _no_client(monkeypatch)

    argv = ["--locale", "fr-CA", "--dry-run", "--model", "us.anthropic.claude-other"]
    assert MODULE.main(argv, layout=layout) == 2
    assert "has no entry in pricing.yaml, so the run cannot be priced or capped" in (
        capsys.readouterr().err
    )


def test_the_real_pricing_file_prices_the_default_model():
    rates = MODULE.read_rates(
        REPO_ROOT / "config_library" / "pricing.yaml", MODULE.DEFAULT_MODEL
    )
    assert rates == MODULE.Rates(
        input=4e-6, output=2e-5, cache_read=2e-7, cache_write=5e-6
    )


# --- AWS errors, paths and usage ---------------------------------------------------------


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "no"}}, "Converse")


def test_an_aws_error_before_any_review_writes_no_report(tmp_path, monkeypatch, capsys):
    layout, _ = _tree(tmp_path, monkeypatch)

    def refuse(request):
        raise _client_error("AccessDeniedException")

    client = FakeBedrock(refuse)
    _use_client(monkeypatch, client)

    assert _run(layout, tmp_path) == 2

    err = capsys.readouterr().err
    assert "Bedrock refused the run, so no report was written" in err
    ceiling = batch_ceiling(client.requests[0])
    assert f"(its attempts count up to US${ceiling:.2f})" in err
    assert not (tmp_path / "out").exists()


def test_an_aws_error_after_a_batch_writes_a_partial_report(tmp_path, monkeypatch):
    en, fr = _many(41)
    layout, _ = _tree(tmp_path, monkeypatch, en=en, fr=fr)
    answers = iter([agree, None])

    def answer(request):
        current = next(answers)
        if current is None:
            raise _client_error("ThrottlingException")
        return current(request)

    client = FakeBedrock(answer)
    _use_client(monkeypatch, client)

    assert _run(layout, tmp_path) == 1

    record = _report(tmp_path)
    assert record["counts"]["same"] == 40
    assert record["counts"]["skipped"] == 1
    assert "ThrottlingException" in record["stopped"]
    # The call that failed after its retries counts at its batch's ceiling.
    failed = batch_ceiling(client.requests[1])
    used = 1_000 * 4e-6 + 500 * 2e-5 + 3_000 * 5e-6
    assert record["cost"]["usd"] == round(used + failed, 4)
    assert record["cost"]["retriedUsd"] == round(failed, 4)
    assert record["cost"]["retriedAttempts"] == MODULE.ATTEMPTS
    assert record["cost"]["calls"] == 1


def test_an_unknown_profile_is_a_tool_error_not_a_traceback(
    tmp_path, monkeypatch, capsys
):
    layout, _ = _tree(tmp_path, monkeypatch)

    def make_client(region, profile):
        raise ProfileNotFound(profile=profile)

    monkeypatch.setattr(MODULE, "make_client", make_client)

    assert _run(layout, tmp_path, "--profile", "nope") == 2
    assert "The config profile (nope) could not be found" in capsys.readouterr().err


def test_a_report_under_src_is_refused_before_anything_runs(
    tmp_path, monkeypatch, capsys
):
    layout, calls = _tree(tmp_path, monkeypatch)
    _no_client(monkeypatch)

    argv = [*RUN, "--out", str(layout.catalog_dir / "review")]
    assert MODULE.main(argv, layout=layout) == 2

    assert "is under src/, which ships" in capsys.readouterr().err
    assert calls == []


def test_the_default_report_goes_under_test_reports(tmp_path, monkeypatch):
    layout, _ = _tree(tmp_path, monkeypatch)
    _use_client(monkeypatch, FakeBedrock())
    monkeypatch.setattr(
        MODULE,
        "now",
        lambda: MODULE.datetime(2030, 1, 2, 3, 4, 5, tzinfo=MODULE.timezone.utc),
    )

    assert MODULE.main(RUN, layout=layout) == 0

    directory = layout.reports_dir / "fr-CA-20300102T030405Z"
    assert sorted(p.name for p in directory.iterdir()) == ["report.json", "report.md"]
    record = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert record["date"] == "2030-01-02T03:04:05Z"
    assert record["glossary"]["terms"] == 2
    assert record["glossary"]["sha256"] == hashlib.sha256(GLOSSARY.encode()).hexdigest()


def test_a_table_cell_cannot_break_the_markdown():
    assert MODULE.cell("a | b\nc \\ d") == "a \\| b<br>c \\\\ d"


@pytest.mark.parametrize(
    "argv",
    [
        ["--locale", "fr-CA"],
        ["--locale", "fr-CA", "--dry-run", "--limit", "0"],
        ["--locale", "fr-CA", "--dry-run", "--max-usd", "0"],
    ],
    ids=["no region outside a dry run", "a limit below one", "a cap of nothing"],
)
def test_usage_errors_exit_2(argv, tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        MODULE.main(argv, layout=MODULE.Layout(tmp_path))
    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "setup, argv, reason",
    [
        (
            lambda layout: None,
            ["--locale", "de", "--dry-run"],
            "there are no review rules for de",
        ),
        (
            lambda layout: layout.catalog("fr-CA").unlink(),
            ["--locale", "fr-CA", "--dry-run"],
            "there is no src/ui/src/i18n/fr-CA.json to review",
        ),
        (
            lambda layout: layout.parser_package.rmdir(),
            ["--locale", "fr-CA", "--dry-run"],
            "run make setup-node first",
        ),
        (
            lambda layout: layout.glossary("fr-CA").write_text(
                "en,fr,note\na,b,c\n", encoding="utf-8"
            ),
            ["--locale", "fr-CA", "--dry-run"],
            "the header must start with en,fr-CA",
        ),
        (
            lambda layout: layout.glossary("fr-CA").write_text(
                "en,fr-CA,note\n", encoding="utf-8"
            ),
            ["--locale", "fr-CA", "--dry-run"],
            "the glossary has no terms",
        ),
    ],
    ids=[
        "no rules",
        "no catalog",
        "no toolchain",
        "a glossary for another locale",
        "an empty glossary",
    ],
)
def test_what_cannot_be_reviewed_is_a_tool_error(
    tmp_path, monkeypatch, capsys, setup, argv, reason
):
    layout, _ = _tree(tmp_path, monkeypatch)
    setup(layout)
    _no_client(monkeypatch)

    assert MODULE.main(argv, layout=layout) == 2
    assert reason in capsys.readouterr().err


def test_without_a_glossary_the_review_runs_and_says_so(tmp_path, monkeypatch, capsys):
    layout, _ = _tree(tmp_path, monkeypatch, glossary=None)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, tmp_path) == 0

    assert "reviewing without a glossary" in capsys.readouterr().out
    assert "Glossary" not in client.requests[0]["system"][0]["text"]
    assert _report(tmp_path)["glossary"] is None


def test_a_glossary_with_a_byte_order_mark_is_read(tmp_path):
    path = tmp_path / "glossary.fr-CA.csv"
    path.write_bytes(("\ufeff" + GLOSSARY).encode("utf-8"))
    glossary = MODULE.read_glossary(path, "fr-CA")
    assert glossary.rows[0] == ("document", "document", "Noun.")
    assert glossary.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_client_is_built_for_bedrock_runtime_with_a_long_read_timeout():
    client = MODULE.make_client("ca-central-1", None)
    assert client.meta.service_model.service_name == "bedrock-runtime"
    assert client.meta.region_name == "ca-central-1"
    assert client.meta.config.read_timeout == 900
    assert client.meta.config.retries["total_max_attempts"] == MODULE.ATTEMPTS


class _Body:
    """The raw body of an HTTP response the test makes up."""

    def __init__(self, data: bytes):
        self.data = data

    def stream(self, **kwargs):
        yield self.data


def test_botocore_retries_a_read_timeout_and_the_run_counts_every_attempt(
    tmp_path, monkeypatch, capsys
):
    """The client ``make_client`` builds, with made-up credentials and its HTTP send
    replaced, so nothing leaves the machine: the first batch times out once and is then
    answered, and the second times out on every attempt botocore makes."""
    from botocore.awsrequest import AWSResponse
    from botocore.exceptions import ReadTimeoutError

    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))

    def no_network(self, request):
        raise AssertionError(f"a request left the machine: {request.url}")

    monkeypatch.setattr("botocore.httpsession.URLLib3Session.send", no_network)
    monkeypatch.setattr(  # no backoff between the attempts
        "botocore.retries.standard.ExponentialBackoff.delay_amount",
        lambda self, context: 0,
    )

    en, fr = _many(41)
    layout, _ = _tree(tmp_path, monkeypatch, en=en, fr=fr)
    attempts: list[dict[str, Any]] = []
    times_out = iter([True, False, *[True] * MODULE.ATTEMPTS])

    def send(request, **kwargs):
        body = json.loads(request.body)
        attempts.append(body)
        if next(times_out):
            raise ReadTimeoutError(endpoint_url=request.url)
        answer = reply([review(i) for i in sent(body)], retries=None)
        data = json.dumps(answer).encode("utf-8")
        return AWSResponse(
            request.url, 200, {"Content-Type": "application/json"}, _Body(data)
        )

    make_client = MODULE.make_client

    def client_that_sends_nothing(region, profile):
        client = make_client(region, profile)
        client.meta.events.register("before-send.bedrock-runtime.Converse", send)
        return client

    monkeypatch.setattr(MODULE, "make_client", client_that_sends_nothing)

    assert _run(layout, tmp_path) == 1

    assert len(attempts) == 2 + MODULE.ATTEMPTS
    # botocore reports the first batch's one retry (RetryAttempts 1; a response that did
    # not say would count ATTEMPTS - 1), and the second batch counts at its ceiling.
    retried = batch_ceiling(attempts[0]) / MODULE.ATTEMPTS + batch_ceiling(attempts[2])
    record = _report(tmp_path)
    assert record["counts"]["same"] == 40
    assert "Read timeout on endpoint URL" in record["stopped"]
    used = 1_000 * 4e-6 + 500 * 2e-5 + 3_000 * 5e-6
    assert record["cost"]["usd"] == round(used + retried, 4)
    assert record["cost"]["retriedUsd"] == round(retried, 4)
    assert record["cost"]["retriedAttempts"] == 1 + MODULE.ATTEMPTS
    note = (
        f"US${retried:.2f} of the cost counts {1 + MODULE.ATTEMPTS} retried or failed"
    )
    assert note in capsys.readouterr().out


# --- end to end, with the real coverage worker -------------------------------------


@dataclass(frozen=True)
class TempCatalog(MODULE.Layout):
    """The real checkout's worker and toolchain over a catalog directory of the test's own."""

    directory: Path = Path()

    @property
    def catalog_dir(self) -> Path:
        return self.directory / "i18n"

    @property
    def pricing(self) -> Path:
        return self.repo_root / "config_library" / "pricing.yaml"


def test_end_to_end_the_build_parity_check_decides_what_is_sent(tmp_path, monkeypatch):
    layout = TempCatalog(REPO_ROOT, directory=tmp_path)
    if not layout.parser_package.is_dir():
        if IN_CI:
            pytest.fail(
                f"{layout.parser_package} is missing; CI installs the UI's dependencies"
            )
        pytest.skip("the UI's node_modules are not installed (run make setup-node)")
    layout.catalog_dir.mkdir()
    layout.catalog("en").write_text(json.dumps(EN), encoding="utf-8")
    fr = _fr()
    fr["nav.greeting"]["message"] = "Bonjour"  # the argument is lost: invalid
    fr["nav.upload"]["sourceHash"] = "0123456789abcdef"  # the English moved: stale
    layout.catalog("fr-CA").write_text(json.dumps(fr), encoding="utf-8")
    layout.glossary("fr-CA").write_text(GLOSSARY, encoding="utf-8")
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert MODULE.main([*RUN, "--out", str(tmp_path / "out")], layout=layout) == 0

    assert [item["id"] for item in sent(client.requests[0])] == [
        "nav.count",
        "nav.queue",
    ]
    record = _report(tmp_path)
    assert record["invalid"] == ["nav.greeting"]
    assert record["stale"] == ["nav.upload"]
    assert any("missing argument(s) {name}" in p for p in record["invalidProblems"])


# --- one translation run's provenance ------------------------------------------------


def _span(slot: int, text: str) -> str:
    return f'<span translate="no" data-ph="{slot}">{text}</span>'


def _provenance(entries: list[dict[str, Any]], locale: str = "fr-CA") -> dict[str, Any]:
    """A provenance file as `make ui-i18n-translate` writes it."""
    return {
        "engine": "Amazon Translate",
        "operation": "TranslateText",
        "region": "ca-central-1",
        "sourceLanguage": "en",
        "targetLanguage": locale,
        "formality": "FORMAL",
        "terminology": {
            "name": "idp-ui-fr-CA",
            "sha256": "ab" * 32,
            "glossary": "src/ui/src/i18n/glossary.fr-CA.csv",
            "terms": 2,
        },
        "date": "2030-01-02T03:04:05Z",
        "characters": {"translated": 60, "backTranslated": 70},
        "backTranslationError": None,
        "entries": entries,
    }


PROVENANCE_ENTRIES = [
    {
        "id": "nav.count",
        "english": EN["nav.count"]["defaultMessage"],
        "stale": False,
        "sentences": [
            {
                "sent": f"{_span(0, '#')} document",
                "answer": f"{_span(0, '#')} document",
                "appliedTerms": [],
                "backTranslation": f"{_span(0, '#')} document",
            },
            {
                "sent": f"{_span(0, '#')} documents",
                "answer": f"{_span(0, '#')} documents",
                "appliedTerms": [],
                "backTranslation": f"{_span(0, '#')} documents",
            },
        ],
        "message": FR_TEXT["nav.count"],
    },
    {
        "id": "nav.greeting",
        "english": "Hello {name}",
        "stale": False,
        "sentences": [
            {
                "sent": f"Hello {_span(0, '{name}')}",
                "answer": f"Bonjour {_span(0, '{name}')}",
                "appliedTerms": [],
                "backTranslation": f"Good day {_span(0, '{name}')} &amp; welcome",
            }
        ],
        "message": "Bonjour {name}",
    },
    {
        "id": "nav.upload",
        "english": "Upload documents",
        "stale": False,
        "sentences": [
            {
                "sent": "Upload documents",
                "answer": "Téléverser des documents",
                "appliedTerms": [],
            }
        ],
        "message": "Téléverser des documents",
    },
    {
        # Left out by that run: the screen shows the English, so nothing to review.
        "id": "nav.queue",
        "english": "Open the Review Queue",
        "stale": False,
        "sentences": [
            {"sent": "Open the Review Queue", "answer": "Ouvrir", "appliedTerms": []}
        ],
        "errors": ["nav.queue: the answer lost span(s) 0"],
    },
    {
        # Written by that run, gone from the catalog since.
        "id": "nav.gone",
        "english": "Gone",
        "stale": False,
        "sentences": [{"sent": "Gone", "answer": "Parti", "appliedTerms": []}],
        "message": "Parti",
    },
]


def _with_provenance(tmp_path: Path, record: Any) -> Path:
    path = tmp_path / "provenance.json"
    path.write_text(
        record if isinstance(record, str) else json.dumps(record), encoding="utf-8"
    )
    return path


def test_a_provenance_file_narrows_the_review_to_the_entries_its_run_wrote(
    tmp_path, monkeypatch, capsys
):
    layout, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock()
    _use_client(monkeypatch, client)
    path = _with_provenance(tmp_path, _provenance(PROVENANCE_ENTRIES))

    assert _run(layout, tmp_path, "--provenance", str(path)) == 0

    assert [item["id"] for item in sent(client.requests[0])] == [
        "nav.count",
        "nav.greeting",
        "nav.upload",
    ]
    assert (
        "1 entry that run wrote is no longer in fr-CA.json" in capsys.readouterr().out
    )


def test_back_translations_reach_the_model_as_plain_text(tmp_path, monkeypatch):
    layout, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock()
    _use_client(monkeypatch, client)
    path = _with_provenance(tmp_path, _provenance(PROVENANCE_ENTRIES))

    assert _run(layout, tmp_path, "--provenance", str(path)) == 0

    by_id = {item["id"]: item for item in sent(client.requests[0])}
    assert by_id["nav.greeting"]["backTranslation"] == ["Good day {name} & welcome"]
    assert by_id["nav.count"]["backTranslation"] == ["# document", "# documents"]
    assert "backTranslation" not in by_id["nav.upload"]
    assert "It is evidence only" in client.requests[0]["system"][0]["text"]


def test_the_report_records_the_provenance(tmp_path, monkeypatch):
    layout, _ = _tree(tmp_path, monkeypatch)
    _use_client(monkeypatch, FakeBedrock())
    path = _with_provenance(tmp_path, _provenance(PROVENANCE_ENTRIES))

    assert _run(layout, tmp_path, "--provenance", str(path)) == 0

    record = _report(tmp_path)
    assert record["provenance"] == {
        "path": "provenance.json",
        "date": "2030-01-02T03:04:05Z",
        "terminologySha256": "ab" * 32,
        "written": 4,
        "notInCatalog": 1,
    }
    greeting = next(e for e in record["entries"] if e["id"] == "nav.greeting")
    assert greeting["backTranslation"] == ["Good day {name} & welcome"]
    markdown = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert (
        "Only the entries written by the translation run of 2030-01-02T03:04:05Z "
        "(`provenance.json`)." in markdown
    )


def test_a_branch_without_a_back_translation_drops_the_entrys_evidence(
    tmp_path, monkeypatch
):
    entries = json.loads(json.dumps(PROVENANCE_ENTRIES))
    entries[0]["sentences"][1]["backTranslation"] = None  # the cap stopped it there
    layout, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock()
    _use_client(monkeypatch, client)
    path = _with_provenance(tmp_path, _provenance(entries))

    assert _run(layout, tmp_path, "--provenance", str(path)) == 0

    by_id = {item["id"]: item for item in sent(client.requests[0])}
    assert "backTranslation" not in by_id["nav.count"]
    assert by_id["nav.greeting"]["backTranslation"] == ["Good day {name} & welcome"]


def test_a_branch_never_sent_stands_for_itself():
    texts = MODULE.branch_texts(
        [
            {"sent": None, "answer": _span(0, "#")},
            {
                "sent": f"{_span(0, '#')} items",
                "answer": f"{_span(0, '#')} éléments",
                "backTranslation": f"{_span(0, '#')} elements",
            },
        ]
    )
    assert texts == ["#", "# elements"]


def test_an_entry_edited_since_that_run_is_reviewed_without_its_old_evidence(
    tmp_path, monkeypatch
):
    texts = dict(FR_TEXT, **{"nav.greeting": "Bonjour, {name}"})
    layout, _ = _tree(tmp_path, monkeypatch, fr=_fr(texts))
    client = FakeBedrock()
    _use_client(monkeypatch, client)
    path = _with_provenance(tmp_path, _provenance(PROVENANCE_ENTRIES))

    assert _run(layout, tmp_path, "--provenance", str(path)) == 0

    by_id = {item["id"]: item for item in sent(client.requests[0])}
    assert by_id["nav.greeting"]["translation"] == "Bonjour, {name}"
    assert "backTranslation" not in by_id["nav.greeting"]
    assert by_id["nav.count"]["backTranslation"] == ["# document", "# documents"]


@pytest.mark.parametrize(
    "record, reason",
    [
        (
            _provenance(PROVENANCE_ENTRIES, locale="fr"),
            "records a run for 'fr', not fr-CA",
        ),
        ("{not json", "provenance.json could not be read"),
        (
            {"targetLanguage": "fr-CA"},
            "is not the provenance file of a translation run",
        ),
        (_provenance([{"message": "x"}]), "holds an entry without an id"),
    ],
    ids=["another locale", "not JSON", "no entries", "an entry without an id"],
)
def test_a_file_that_is_no_provenance_of_this_locale_is_refused(
    tmp_path, monkeypatch, capsys, record, reason
):
    layout, _ = _tree(tmp_path, monkeypatch)
    _no_client(monkeypatch)
    path = _with_provenance(tmp_path, record)

    argv = ["--locale", "fr-CA", "--dry-run", "--provenance", str(path)]
    assert MODULE.main(argv, layout=layout) == 2
    assert reason in capsys.readouterr().err
