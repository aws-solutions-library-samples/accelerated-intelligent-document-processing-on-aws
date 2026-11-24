# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ui_i18n_revise.py: the revision of the entries a review flagged.

Bedrock is a fake, and the coverage worker a stand-in that checks arguments and
``sourceHash`` the way the build's parity check does, except in the end-to-end test,
which runs the real worker over a catalog directory of its own.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Same rule as ``IN_CI`` in ``test_ui_i18n_review.py``.
IN_CI = bool(os.getenv("CI") or os.getenv("GITLAB_CI") or os.getenv("GITHUB_ACTIONS"))


def _load_module():
    """Load `scripts/ui_i18n_revise.py` by path (scripts/ is not a package)."""
    path = REPO_ROOT / "scripts" / "ui_i18n_revise.py"
    spec = importlib.util.spec_from_file_location("_ui_i18n_revise", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()
REVIEW = MODULE.review

NBSP = "\u00a0"
STARTED = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def source_hash(text: str) -> str:
    """The catalog's ``sourceHash``, restated so the test catalogs are well formed."""
    digest = hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8"))
    return digest.hexdigest()[:16]


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
    "nav.empty": {"defaultMessage": "'<No content>'", "description": "An empty step"},
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

#: The catalog before the run: ``nav.empty`` has no translation yet.
FR_TEXT = {
    "nav.count": "{count,plural,one{# document} other{# documents}}",
    "nav.greeting": "Salut {name}",
    "nav.queue": "Ouvrir la file d’attente d’examen",
    "nav.upload": "Téléverser des documents",
}

GLOSSARY = (
    "en,fr-CA,note\n"
    "document,document,Noun.\n"
    "review queue,file d’attente de révision,Noun phrase.\n"
)

GREETING_FINDINGS = {
    "problems": ["register"],
    "reason": "Salut is familiar.",
    "suggestion": "Bonjour {name}",
}


def _fr(texts: dict[str, str] = FR_TEXT, en=EN) -> dict[str, Any]:
    return {
        entry_id: {
            "message": text,
            "sourceHash": source_hash(en[entry_id]["defaultMessage"]),
        }
        for entry_id, text in texts.items()
    }


def _report_entry(entry_id: str, outcome: str, **fields) -> dict[str, Any]:
    entry = {
        "id": entry_id,
        "english": EN[entry_id]["defaultMessage"],
        "description": EN[entry_id]["description"],
        "translation": FR_TEXT[entry_id],
        "glossaryTerms": [],
        "backTranslation": [],
        "outcome": outcome,
        "problems": [],
        "reason": "",
        "rendering": "",
    }
    entry.update(fields)
    return entry


def _review_report(entries=None, *, locale="fr-CA", tool="ui-i18n-review") -> dict:
    """A review's report.json, as `make ui-i18n-review` writes it."""
    return {
        "tool": tool,
        "locale": locale,
        "entries": entries
        if entries is not None
        else [
            _report_entry("nav.count", "same-meaning"),
            _report_entry(
                "nav.greeting",
                "disagreement",
                problems=GREETING_FINDINGS["problems"],
                reason=GREETING_FINDINGS["reason"],
                rendering=GREETING_FINDINGS["suggestion"],
            ),
            _report_entry(
                "nav.queue", "unreviewed", reason="the model gave no answer for it"
            ),
            _report_entry("nav.upload", "same"),
        ],
    }


def _coverage(locale="fr-CA", invalid=(), stale=(), problems=()) -> str:
    return json.dumps(
        {
            "sourceKeys": len(EN),
            "locales": {
                locale: {
                    "translated": 0,
                    "missing": [],
                    "stale": list(stale),
                    "invalid": list(invalid),
                    "problems": list(problems),
                    "dead": [],
                    "coveragePercent": 0,
                }
            },
        }
    )


def _arguments(text: str) -> list[str]:
    return sorted(set(re.findall(r"\{(\w+)", text)))


def _check_answers(directory: Path, en) -> str:
    """The stand-in parity check: an argument lost or added is invalid, and a wrong
    sourceHash is stale."""
    answers = json.loads((directory / "fr-CA.json").read_text(encoding="utf-8"))
    invalid, stale, problems = [], [], []
    for entry_id, entry in sorted(answers.items()):
        english = en[entry_id]["defaultMessage"]
        if _arguments(entry["message"]) != _arguments(english):
            invalid.append(entry_id)
            problems.append(
                f"fr-CA.json: {entry_id}: the translation is missing argument(s) "
                f"{{{', '.join(_arguments(english))}}}"
            )
        elif entry["sourceHash"] != source_hash(english):
            stale.append(entry_id)
    return _coverage(invalid=invalid, stale=stale, problems=problems)


def _tree(
    tmp_path: Path,
    monkeypatch,
    *,
    en=EN,
    fr=None,
    glossary: str | None = GLOSSARY,
    planned: str | None = None,
    report: Any = None,
    notes: Any = None,
):
    """A checkout with a review report and notes; returns it, the files, and the worker calls."""
    layout = REVIEW.Layout(tmp_path)
    layout.catalog_dir.mkdir(parents=True)
    layout.parser_package.mkdir(parents=True)
    layout.pricing.parent.mkdir(parents=True)
    layout.pricing.write_text(PRICING, encoding="utf-8")
    layout.catalog("en").write_text(json.dumps(en), encoding="utf-8")
    layout.catalog("fr-CA").write_text(
        json.dumps(fr if fr is not None else _fr(en=en), ensure_ascii=False),
        encoding="utf-8",
    )
    if glossary is not None:
        layout.glossary("fr-CA").write_text(glossary, encoding="utf-8")
    report_path = tmp_path / "review" / "report.json"
    report_path.parent.mkdir()
    report_path.write_text(
        json.dumps(_review_report() if report is None else report), encoding="utf-8"
    )
    notes_path = tmp_path / "notes.json"
    notes_path.write_text(
        json.dumps(
            {"nav.empty": "Translate never translated it."} if notes is None else notes
        ),
        encoding="utf-8",
    )
    calls: list[Path] = []

    def run(command, **kwargs):
        directory = Path(command[2])
        calls.append(directory)
        if directory == layout.catalog_dir:
            stdout = planned or _coverage()
        else:
            stdout = _check_answers(directory, en)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(REVIEW, "run", run)
    monkeypatch.setattr(REVIEW, "now", lambda: STARTED)
    return layout, report_path, notes_path, calls


# --- a fake Bedrock ----------------------------------------------------------------

USAGE = {
    "inputTokens": 1_000,
    "outputTokens": 500,
    "totalTokens": 4_500,
    "cacheReadInputTokens": 0,
    "cacheWriteInputTokens": 3_000,
}


def reply(revisions, *, usage: Any = USAGE, stop="tool_use", text: str | None = None):
    """A Converse response: thinking, optional prose, and the revision tool's call."""
    content: list[dict[str, Any]] = [
        {"reasoningContent": {"reasoningText": {"text": "Looking at each message."}}}
    ]
    if text is not None:
        content.append({"text": text})
    if revisions is not None:
        content.append(
            {
                "toolUse": {
                    "toolUseId": "tooluse-1",
                    "name": "record_revision",
                    "input": {"revisions": revisions},
                }
            }
        )
    response: dict[str, Any] = {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop,
    }
    if usage is not None:
        response["usage"] = usage
    return response


def sent(request) -> list[dict[str, Any]]:
    """The entries one request carried."""
    text = request["messages"][0]["content"][0]["text"]
    return json.loads(text.split("\n", 1)[1])


#: What the fake model writes per id; any other id keeps its current text.
ANSWERS = {
    "nav.empty": "'<Aucun contenu>'",
    "nav.greeting": "Bonjour {name}",
    "nav.queue": "Ouvrir la file d’attente de révision",
}


def answer_with(answers=ANSWERS):
    def answer(request):
        return reply(
            [
                {
                    "id": item["id"],
                    "message": answers.get(item["id"], item.get("translation", "x")),
                }
                for item in sent(request)
            ]
        )

    return answer


class FakeBedrock:
    """Stands in for a boto3 bedrock-runtime client: records requests, answers through ``answer``."""

    def __init__(self, answer=None):
        self.answer = answer or answer_with()
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request):
        self.requests.append(request)
        return self.answer(request)


def _use_client(monkeypatch, client) -> list[tuple[str, str | None]]:
    asked: list[tuple[str, str | None]] = []

    def make_client(region, profile):
        asked.append((region, profile))
        return client

    monkeypatch.setattr(REVIEW, "make_client", make_client)
    return asked


def _no_client(monkeypatch) -> None:
    def make_client(region, profile):
        raise AssertionError("no AWS client may be built on this path")

    monkeypatch.setattr(REVIEW, "make_client", make_client)


RUN = ["--locale", "fr-CA", "--region", "ca-central-1"]


def _run(layout, report_path, notes_path, *extra: str) -> int:
    argv = [*RUN, "--review", str(report_path), "--notes", str(notes_path), *extra]
    return MODULE.main(argv, layout=layout)


def _catalog(layout) -> dict[str, Any]:
    return json.loads(layout.catalog("fr-CA").read_text(encoding="utf-8"))


def _provenance_path(layout) -> Path:
    return (
        layout.repo_root
        / "test-reports"
        / "ui-i18n-revise"
        / "fr-CA-20300102T030405Z.json"
    )


def _provenance(layout) -> dict[str, Any]:
    return json.loads(_provenance_path(layout).read_text(encoding="utf-8"))


def _outcomes(record) -> dict[str, str]:
    return {entry["id"]: entry["outcome"] for entry in record["entries"]}


# --- what is revised ----------------------------------------------------------------


def test_a_dry_run_prints_the_plan_and_the_estimate_and_calls_nothing(
    tmp_path, monkeypatch, capsys
):
    layout, report_path, notes_path, calls = _tree(tmp_path, monkeypatch)
    _no_client(monkeypatch)
    before = layout.catalog("fr-CA").read_text(encoding="utf-8")

    argv = [
        "--locale",
        "fr-CA",
        "--dry-run",
        "--review",
        str(report_path),
        "--notes",
        str(notes_path),
    ]
    assert MODULE.main(argv, layout=layout) == 0

    out = capsys.readouterr().out
    assert (
        "fr-CA: 3 entries to revise in 1 batch with us.anthropic.claude-opus-5-5; about US$"
        in out
    )
    assert "(cap US$20.00)" in out
    assert "left out: 0 invalid, 0 stale, 0 edited after their review" in out
    assert calls == [layout.catalog_dir]
    assert not (layout.repo_root / "test-reports").exists()
    assert layout.catalog("fr-CA").read_text(encoding="utf-8") == before


def test_the_targets_are_the_flagged_entries_and_the_noted_ones(tmp_path, monkeypatch):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, report_path, notes_path) == 0

    # The disagreement, the entry the review could not review, and the noted entry the
    # catalog lacks; never the entries the review agreed with.
    assert [item["id"] for item in sent(client.requests[0])] == [
        "nav.empty",
        "nav.greeting",
        "nav.queue",
    ]


def test_each_entry_carries_the_findings_its_note_and_its_glossary_terms(
    tmp_path, monkeypatch
):
    layout, report_path, notes_path, _ = _tree(
        tmp_path,
        monkeypatch,
        notes={"nav.empty": "", "nav.queue": "Shown as « examen »."},
    )
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, report_path, notes_path) == 0

    by_id = {item["id"]: item for item in sent(client.requests[0])}
    assert by_id["nav.greeting"] == {
        "id": "nav.greeting",
        "english": "Hello {name}",
        "description": "Greeting",
        "translation": "Salut {name}",
        "review": GREETING_FINDINGS,
    }
    assert by_id["nav.queue"]["glossaryTerms"] == ["review queue"]
    assert by_id["nav.queue"]["note"] == "Shown as « examen »."
    assert "review" not in by_id["nav.queue"]  # unreviewed: there are no findings
    assert "translation" not in by_id["nav.empty"]  # the catalog has none
    assert "note" not in by_id["nav.empty"]  # an empty note adds the entry only


def test_the_prefix_asks_for_canadian_french_and_holds_the_rules_and_the_glossary(
    tmp_path, monkeypatch
):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, report_path, notes_path) == 0

    request = client.requests[0]
    prefix = request["system"][0]["text"]
    assert "Canadian French wherever it fits" in prefix
    assert "courriel, clavardage, téléverser and bogue" in prefix
    assert "no space before ; ? and !" in prefix
    assert REVIEW.STYLE_RULES["fr-CA"] in prefix
    assert "review queue,file d’attente de révision,Noun phrase." in prefix
    assert "'<No content>'" in prefix
    assert request["system"][1] == {"cachePoint": {"type": "default"}}
    # Shaped for Claude Opus 5.5, as the review's request.
    assert "temperature" not in request["inferenceConfig"]
    assert request["toolConfig"]["toolChoice"] == {"auto": {}}
    assert request["additionalModelRequestFields"] == {
        "output_config": {"effort": "low"}
    }
    assert request["toolConfig"]["tools"][0]["toolSpec"]["name"] == "record_revision"


def test_an_entry_edited_after_its_review_is_left_out(tmp_path, monkeypatch, capsys):
    texts = dict(FR_TEXT, **{"nav.greeting": "Bonjour, {name}"})
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch, fr=_fr(texts))
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, report_path, notes_path) == 0

    assert [item["id"] for item in sent(client.requests[0])] == [
        "nav.empty",
        "nav.queue",
    ]
    assert "1 edited after their review" in capsys.readouterr().out
    assert _provenance(layout)["editedAfterReview"] == ["nav.greeting"]
    assert _catalog(layout)["nav.greeting"]["message"] == "Bonjour, {name}"


def test_invalid_and_stale_entries_are_listed_and_never_sent(tmp_path, monkeypatch):
    planned = _coverage(invalid=["nav.greeting"], stale=["nav.queue"])
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch, planned=planned)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, report_path, notes_path) == 0

    assert [item["id"] for item in sent(client.requests[0])] == ["nav.empty"]
    record = _provenance(layout)
    assert (record["invalid"], record["stale"]) == (["nav.greeting"], ["nav.queue"])


def test_limit_takes_the_first_entries_in_id_order(tmp_path, monkeypatch, capsys):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, report_path, notes_path, "--limit", "2") == 0

    assert [item["id"] for item in sent(client.requests[0])] == [
        "nav.empty",
        "nav.greeting",
    ]
    assert "1 more left for a later run by --limit" in capsys.readouterr().out


def test_batches_hold_at_most_40_entries_and_12000_characters():
    def target(n: int, size: int):
        return MODULE.Target(
            id=f"m.{n}", english="x" * size, description="", before=None, terms=[]
        )

    small = [target(n, 10) for n in range(85)]
    assert [len(batch) for batch in MODULE.make_batches(small)] == [40, 40, 5]

    large = [target(n, 5_000) for n in range(5)]
    assert [len(batch) for batch in MODULE.make_batches(large)] == [2, 2, 1]


# --- what is written ----------------------------------------------------------------


def test_an_answer_that_passes_is_written_with_its_sourcehash(
    tmp_path, monkeypatch, capsys
):
    layout, report_path, notes_path, calls = _tree(tmp_path, monkeypatch)
    _use_client(monkeypatch, FakeBedrock())

    assert _run(layout, report_path, notes_path) == 0

    catalog = _catalog(layout)
    assert catalog["nav.greeting"] == {
        "message": "Bonjour {name}",
        "sourceHash": source_hash("Hello {name}"),
    }
    assert catalog["nav.empty"] == {
        "message": "'<Aucun contenu>'",
        "sourceHash": source_hash("'<No content>'"),
    }
    assert (
        catalog["nav.upload"]["message"] == "Téléverser des documents"
    )  # not a target
    text = layout.catalog("fr-CA").read_text(encoding="utf-8")
    assert (
        text == json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    # The plan's worker call, then the parity check of the answers in a scratch catalog.
    assert len(calls) == 2 and calls[1] != layout.catalog_dir
    assert (
        "3 revised, 0 unchanged, 0 refused, 0 skipped of 3" in capsys.readouterr().out
    )


def test_an_answer_the_parity_check_refuses_keeps_the_current_text(
    tmp_path, monkeypatch, capsys
):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    _use_client(
        monkeypatch,
        FakeBedrock(answer_with(dict(ANSWERS, **{"nav.greeting": "Bonjour"}))),
    )

    assert _run(layout, report_path, notes_path) == 1

    assert _catalog(layout)["nav.greeting"]["message"] == "Salut {name}"
    greeting = next(
        e for e in _provenance(layout)["entries"] if e["id"] == "nav.greeting"
    )
    assert greeting["outcome"] == "refused"
    assert greeting["reason"] == (
        "the build's parity check refused it: fr-CA.json: nav.greeting: the "
        "translation is missing argument(s) {name}"
    )
    assert greeting["after"] == "Bonjour"
    assert "message" not in greeting
    out = capsys.readouterr().out
    assert "refused: nav.greeting: the build's parity check refused it" in out


def test_an_answer_with_the_wrong_hash_would_be_refused_as_stale(tmp_path, monkeypatch):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    _use_client(monkeypatch, FakeBedrock())
    monkeypatch.setattr(MODULE, "source_hash", lambda text: "0123456789abcdef")

    assert _run(layout, report_path, notes_path) == 1

    record = _provenance(layout)
    assert set(_outcomes(record).values()) == {"refused"}
    assert record["entries"][0]["reason"].endswith(
        "its sourceHash is not the English's"
    )
    assert _catalog(layout) == _fr()


def test_an_unchanged_answer_is_counted_and_not_rewritten(tmp_path, monkeypatch):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    answers = dict(ANSWERS, **{"nav.queue": FR_TEXT["nav.queue"]})
    _use_client(monkeypatch, FakeBedrock(answer_with(answers)))

    assert _run(layout, report_path, notes_path) == 0

    record = _provenance(layout)
    assert _outcomes(record)["nav.queue"] == "unchanged"
    assert record["counts"] == {
        "revised": 2,
        "unchanged": 1,
        "refused": 0,
        "skipped": 0,
    }


def test_an_answer_keeps_the_englishs_edges_and_is_nfc():
    assert MODULE.as_written("Hello {name}", " Bonjour {name}\n") == "Bonjour {name}"
    assert MODULE.as_written("Hello ", "Bonjour") == "Bonjour "
    assert MODULE.as_written("Upload", "Te\u0301le\u0301verser") == "Téléverser"


@pytest.mark.parametrize(
    "answer, written",
    [
        ("Statut : prêt", f"Statut{NBSP}: prêt"),
        ("Statut: prêt", f"Statut{NBSP}: prêt"),
        ("Statut\u202f:", f"Statut{NBSP}:"),
        ("{name}: {value}", f"{{name}}{NBSP}: {{value}}"),
        (
            "{count,plural,one{Fichier :} other{Fichiers :}}",
            f"{{count,plural,one{{Fichier{NBSP}:}} other{{Fichiers{NBSP}:}}}}",
        ),
        ("<b>Statut :</b> {value}", f"<b>Statut{NBSP}:</b> {{value}}"),
        (
            "https://example.com à 10:30, {h}:{m}",
            "https://example.com à 10:30, {h}:{m}",
        ),
        ("{share, number, ::percent} gardé", "{share, number, ::percent} gardé"),
        (f"Supprimer ? Oui ! Non ;{NBSP}ici\u202f?", f"Supprimer? Oui! Non;{NBSP}ici?"),
        ("le profil « default »", f"le profil «{NBSP}default{NBSP}»"),
        (f"le profil «{NBSP}default{NBSP}»", f"le profil «{NBSP}default{NBSP}»"),
    ],
    ids=[
        "colon",
        "colon without a space",
        "narrow space",
        "after an argument",
        "end of a branch",
        "before a closing tag",
        "not a phrase end",
        "number skeleton",
        "; ? !",
        "quotation marks",
        "already right",
    ],
)
def test_the_canadian_spacing_is_applied_by_code(answer, written):
    assert MODULE.canadian_spacing(answer) == written
    assert MODULE.as_written("x", answer, "fr-CA") == written
    assert (
        MODULE.as_written("x", answer) == answer.strip()
    )  # another locale keeps the model's


def test_the_written_catalog_gets_the_no_break_spaces_the_model_left_out(
    tmp_path, monkeypatch
):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    answers = dict(ANSWERS, **{"nav.queue": "Ouvrir : la file d’attente de révision"})
    _use_client(monkeypatch, FakeBedrock(answer_with(answers)))

    assert _run(layout, report_path, notes_path) == 0

    assert (
        _catalog(layout)["nav.queue"]["message"]
        == f"Ouvrir{NBSP}: la file d’attente de révision"
    )


def _set(entry_id: str, **fields):
    def answer(request):
        revisions = []
        for item in sent(request):
            revision = {"id": item["id"], "message": ANSWERS[item["id"]]}
            if item["id"] == entry_id:
                revision.update(fields)
            revisions.append(revision)
        return reply(revisions)

    return answer


def _twice(request):
    revisions = [
        {"id": item["id"], "message": ANSWERS[item["id"]]} for item in sent(request)
    ]
    return reply(revisions + [{"id": "nav.greeting", "message": "Bonjour {name}"}])


@pytest.mark.parametrize(
    "answer, refused, reason",
    [
        (
            lambda r: reply(None, text="Here are the revisions."),
            3,
            "answered without the record_revision tool",
        ),
        (lambda r: reply([], stop="max_tokens"), 3, "cut off at the token limit"),
        (_twice, 1, "the model answered for it twice"),
        (_set("nav.greeting", message="  "), 1, "unusable: it has no message"),
        (_set("nav.greeting", message="x" * 4_001), 1, "longer than 4000 characters"),
        (_set("nav.greeting", id="nav.other"), 1, "the model gave no answer for it"),
    ],
    ids=["prose", "cut off", "twice", "empty", "too long", "no answer"],
)
def test_an_unusable_answer_is_refused_with_the_reason(
    tmp_path, monkeypatch, answer, refused, reason
):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    _use_client(monkeypatch, FakeBedrock(answer))

    assert _run(layout, report_path, notes_path) == 1

    record = _provenance(layout)
    chosen = [e for e in record["entries"] if e["outcome"] == "refused"]
    assert len(chosen) == refused
    assert all(reason in e["reason"] for e in chosen)
    assert _catalog(layout)["nav.greeting"]["message"] in (
        "Salut {name}",
        "Bonjour {name}",
    )
    if refused == 3:
        assert _catalog(layout) == _fr()  # nothing passed, so the catalog is as it was


@pytest.mark.parametrize(
    "result, reason",
    [
        ((1, ""), "the coverage worker exited 1: boom"),
        ((0, _coverage(locale="fr")), "the coverage worker reported nothing for fr-CA"),
    ],
    ids=["the worker fails", "the worker reports another locale"],
)
def test_a_parity_check_that_cannot_run_refuses_every_answer_and_writes_none(
    tmp_path, monkeypatch, result, reason
):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    _use_client(monkeypatch, FakeBedrock())
    planned = REVIEW.run

    def run(command, **kwargs):
        if Path(command[2]) == layout.catalog_dir:
            return planned(command, **kwargs)
        code, stdout = result
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="boom")

    monkeypatch.setattr(REVIEW, "run", run)

    assert _run(layout, report_path, notes_path) == 1

    record = _provenance(layout)
    assert set(_outcomes(record).values()) == {"refused"}
    assert record["entries"][0]["reason"] == f"the parity check could not run: {reason}"
    assert (
        record["entries"][0]["after"] == "'<Aucun contenu>'"
    )  # the paid answer is kept
    assert _catalog(layout) == _fr()


def test_a_plan_the_worker_reports_nothing_for_is_a_tool_error(
    tmp_path, monkeypatch, capsys
):
    layout, report_path, notes_path, _ = _tree(
        tmp_path, monkeypatch, planned=_coverage(locale="fr")
    )
    _no_client(monkeypatch)

    assert _run(layout, report_path, notes_path, "--dry-run") == 2
    assert "the coverage worker reported nothing for fr-CA" in capsys.readouterr().err


def test_an_unknown_profile_is_a_tool_error_not_a_traceback(
    tmp_path, monkeypatch, capsys
):
    from botocore.exceptions import ProfileNotFound

    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)

    def make_client(region, profile):
        raise ProfileNotFound(profile=profile)

    monkeypatch.setattr(REVIEW, "make_client", make_client)

    assert _run(layout, report_path, notes_path, "--profile", "nobody") == 2
    assert "no Bedrock client could be made" in capsys.readouterr().err
    assert not (layout.repo_root / "test-reports").exists()


# --- the provenance -----------------------------------------------------------------


def test_the_provenance_records_the_run(tmp_path, monkeypatch, capsys):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    _use_client(
        monkeypatch,
        FakeBedrock(answer_with(dict(ANSWERS, **{"nav.queue": FR_TEXT["nav.queue"]}))),
    )

    assert _run(layout, report_path, notes_path) == 0

    record = _provenance(layout)
    assert {
        k: record[k]
        for k in (
            "tool",
            "locale",
            "targetLanguage",
            "model",
            "region",
            "effort",
            "date",
        )
    } == {
        "tool": "ui-i18n-revise",
        "locale": "fr-CA",
        "targetLanguage": "fr-CA",
        "model": "us.anthropic.claude-opus-5-5",
        "region": "ca-central-1",
        "effort": "low",
        "date": "2030-01-02T03:04:05Z",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", record["promptSha256"])
    assert record["glossary"] == {
        "path": "src/ui/src/i18n/glossary.fr-CA.csv",
        "sha256": hashlib.sha256(GLOSSARY.encode("utf-8")).hexdigest(),
        "terms": 2,
    }
    assert record["reviews"] == ["review/report.json"]
    assert record["notes"] == "notes.json"
    assert record["cost"]["calls"] == 1
    by_id = {entry["id"]: entry for entry in record["entries"]}
    assert by_id["nav.greeting"] == {
        "id": "nav.greeting",
        "english": "Hello {name}",
        "before": "Salut {name}",
        "after": "Bonjour {name}",
        "outcome": "revised",
        "reason": "",
        "flagged": GREETING_FINDINGS,
        "note": None,
        "message": "Bonjour {name}",
    }
    assert by_id["nav.empty"]["before"] is None
    assert by_id["nav.empty"]["note"] == "Translate never translated it."
    assert "message" not in by_id["nav.queue"]  # unchanged: nothing written
    assert "provenance in test-reports/ui-i18n-revise/fr-CA-20300102T030405Z.json" in (
        capsys.readouterr().out
    )


def test_the_review_reads_the_provenance_as_the_entries_this_run_wrote(
    tmp_path, monkeypatch
):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    _use_client(
        monkeypatch,
        FakeBedrock(answer_with(dict(ANSWERS, **{"nav.queue": FR_TEXT["nav.queue"]}))),
    )

    assert _run(layout, report_path, notes_path) == 0

    provenance = REVIEW.read_provenance(_provenance_path(layout), "fr-CA")
    assert provenance.written == {
        "nav.empty": "'<Aucun contenu>'",
        "nav.greeting": "Bonjour {name}",
    }
    assert provenance.back_translations == {}
    assert provenance.date == "2030-01-02T03:04:05Z"


def test_a_provenance_under_src_is_refused_before_anything_runs(
    tmp_path, monkeypatch, capsys
):
    layout, report_path, notes_path, calls = _tree(tmp_path, monkeypatch)
    _no_client(monkeypatch)
    inside = layout.catalog_dir / "provenance.json"

    assert _run(layout, report_path, notes_path, "--provenance", str(inside)) == 2

    assert "is under src/, which ships" in capsys.readouterr().err
    assert calls == []


def test_the_provenance_is_written_before_the_catalog(tmp_path, monkeypatch, capsys):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    _use_client(monkeypatch, FakeBedrock())
    (layout.repo_root / "test-reports").mkdir()
    (layout.repo_root / "test-reports" / "ui-i18n-revise").write_text(
        "a file", encoding="utf-8"
    )

    assert _run(layout, report_path, notes_path) == 2

    assert "the provenance file could not be written, so the catalog was not" in (
        capsys.readouterr().err
    )
    assert _catalog(layout) == _fr()


# --- the cost and the cap -----------------------------------------------------------


def _many(count: int):
    """``count`` noted entries, for batching and the cap."""
    en = {
        f"m.{n:03}": {"defaultMessage": f"Message {n}", "description": "Test"}
        for n in range(count)
    }
    fr = {
        entry_id: {"message": f"Message {n}", "sourceHash": source_hash(f"Message {n}")}
        for n, entry_id in enumerate(en)
    }
    return en, fr, {entry_id: "" for entry_id in en}


def test_an_estimate_over_the_cap_refuses_to_start(tmp_path, monkeypatch, capsys):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    _no_client(monkeypatch)

    assert _run(layout, report_path, notes_path, "--max-usd", "0.001") == 2

    assert "is over --max-usd 0.00" in capsys.readouterr().err
    assert not (layout.repo_root / "test-reports").exists()


def test_a_first_batch_that_could_pass_the_cap_is_never_sent(
    tmp_path, monkeypatch, capsys
):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, report_path, notes_path, "--max-usd", "0.5") == 2

    assert "the first batch could cost up to US$" in capsys.readouterr().err
    assert client.requests == []
    assert not (layout.repo_root / "test-reports").exists()


def test_the_cap_stops_the_run_before_a_batch_that_could_pass_it(
    tmp_path, monkeypatch, capsys
):
    en, fr, notes = _many(50)
    layout, report_path, notes_path, _ = _tree(
        tmp_path, monkeypatch, en=en, fr=fr, report=_review_report([]), notes=notes
    )
    # The first batch costs US$0.42, so the second, at its largest, no longer fits.
    usage = dict(USAGE, outputTokens=20_000)
    client = FakeBedrock(
        lambda r: reply(
            [{"id": i["id"], "message": "Nouveau"} for i in sent(r)], usage=usage
        )
    )
    _use_client(monkeypatch, client)
    first = MODULE.make_batches(
        [
            MODULE.Target(
                id=k,
                english=v["defaultMessage"],
                description="Test",
                before=fr[k]["message"],
                terms=[],
            )
            for k, v in en.items()
        ]
    )
    prefix = MODULE.instructions(
        "fr-CA", REVIEW.read_glossary(layout.glossary("fr-CA"), "fr-CA")
    )
    rates = REVIEW.read_rates(layout.pricing, REVIEW.DEFAULT_MODEL)
    cap = MODULE.ceiling_usd(rates, prefix, first[0]) + 0.1

    assert _run(layout, report_path, notes_path, "--max-usd", f"{cap:.4f}") == 1

    assert len(client.requests) == 1
    record = _provenance(layout)
    assert record["counts"]["revised"] == 40 and record["counts"]["skipped"] == 10
    assert record["stopped"].startswith(
        "the next batch could take the run past --max-usd"
    )
    assert _catalog(layout)["m.000"]["message"] == "Nouveau"
    assert _catalog(layout)["m.049"]["message"] == "Message 49"
    assert "the run stopped early" in capsys.readouterr().out


def test_the_cost_comes_from_the_reported_usage_and_an_unreported_one_counts_at_its_most(
    tmp_path, monkeypatch
):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    _use_client(monkeypatch, FakeBedrock())

    assert _run(layout, report_path, notes_path) == 0

    expected = 1_000 * 4e-6 + 500 * 2e-5 + 3_000 * 5e-6
    assert _provenance(layout)["cost"]["usd"] == pytest.approx(expected, abs=1e-4)

    other = tmp_path / "other"
    other.mkdir()
    layout, report_path, notes_path, _ = _tree(other, monkeypatch)
    answer = answer_with()
    _use_client(monkeypatch, FakeBedrock(lambda r: {**answer(r), "usage": None}))

    assert _run(layout, report_path, notes_path) == 0

    assert _provenance(layout)["cost"]["usd"] > 0.5  # one batch at its ceiling


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "no"}}, "Converse")


def test_an_aws_error_before_any_answer_writes_nothing(tmp_path, monkeypatch, capsys):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)

    def refuse(request):
        raise _client_error("AccessDeniedException")

    _use_client(monkeypatch, FakeBedrock(refuse))

    assert _run(layout, report_path, notes_path) == 2

    assert "Bedrock refused the run, so nothing was written" in capsys.readouterr().err
    assert not (layout.repo_root / "test-reports").exists()
    assert _catalog(layout) == _fr()


def test_an_aws_error_after_a_batch_writes_what_passed(tmp_path, monkeypatch):
    en, fr, notes = _many(50)
    layout, report_path, notes_path, _ = _tree(
        tmp_path, monkeypatch, en=en, fr=fr, report=_review_report([]), notes=notes
    )
    answers = iter(
        [reply([{"id": f"m.{n:03}", "message": "Nouveau"} for n in range(40)])]
    )

    def answer(request):
        try:
            return next(answers)
        except StopIteration:
            raise _client_error("ThrottlingException") from None

    _use_client(monkeypatch, FakeBedrock(answer))

    assert _run(layout, report_path, notes_path) == 1

    record = _provenance(layout)
    assert record["stopped"].startswith("Bedrock refused a call, so the run stopped")
    assert record["counts"] == {
        "revised": 40,
        "unchanged": 0,
        "refused": 0,
        "skipped": 10,
    }
    assert _catalog(layout)["m.039"]["message"] == "Nouveau"


# --- what cannot be revised ---------------------------------------------------------


@pytest.mark.parametrize(
    "report, reason",
    [
        (
            _review_report(tool="ui-i18n-translate"),
            "is not the report.json of a make ui-i18n-review run",
        ),
        (_review_report(locale="fr"), "reviews 'fr', not fr-CA"),
        (_review_report([{"outcome": "same"}]), "holds an entry without an id"),
        ("{not json", "report.json could not be read"),
    ],
    ids=["another tool", "another locale", "an entry without an id", "not JSON"],
)
def test_a_file_that_is_no_review_report_of_this_locale_is_refused(
    tmp_path, monkeypatch, capsys, report, reason
):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    if isinstance(report, str):
        report_path.write_text(report, encoding="utf-8")
    else:
        report_path.write_text(json.dumps(report), encoding="utf-8")
    _no_client(monkeypatch)

    assert _run(layout, report_path, notes_path, "--dry-run") == 2
    assert reason in capsys.readouterr().err


@pytest.mark.parametrize(
    "notes, reason",
    [
        ({"nav.gone": ""}, "the notes name ids en.json does not have: nav.gone"),
        ({"nav.empty": 3}, "the note for nav.empty is not text"),
    ],
    ids=["an unknown id", "a note that is not text"],
)
def test_notes_that_cannot_be_used_are_refused(
    tmp_path, monkeypatch, capsys, notes, reason
):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch, notes=notes)
    _no_client(monkeypatch)

    assert _run(layout, report_path, notes_path, "--dry-run") == 2
    assert reason in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--locale", "fr-CA", "--dry-run"],
        ["--locale", "fr-CA", "--dry-run", "--notes", "n.json", "--limit", "0"],
        ["--locale", "fr-CA", "--dry-run", "--notes", "n.json", "--max-usd", "0"],
        ["--locale", "fr-CA", "--notes", "n.json"],
    ],
    ids=["nothing to revise", "limit 0", "no budget", "no region"],
)
def test_usage_errors_exit_2(argv):
    with pytest.raises(SystemExit) as raised:
        MODULE.main(argv)
    assert raised.value.code == 2


def test_what_cannot_be_revised_is_a_tool_error(tmp_path, monkeypatch, capsys):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch)
    _no_client(monkeypatch)
    argv = ["--locale", "de", "--dry-run", "--notes", str(notes_path)]
    assert MODULE.main(argv, layout=layout) == 2
    assert "there are no revision rules for de" in capsys.readouterr().err

    layout.catalog("fr-CA").unlink()
    assert _run(layout, report_path, notes_path, "--dry-run") == 2
    assert "there is no src/ui/src/i18n/fr-CA.json to revise" in capsys.readouterr().err

    layout.parser_package.rmdir()
    assert _run(layout, report_path, notes_path, "--dry-run") == 2
    assert "run make setup-node first" in capsys.readouterr().err


def test_with_nothing_ready_the_run_calls_nothing(tmp_path, monkeypatch, capsys):
    layout, report_path, notes_path, _ = _tree(
        tmp_path, monkeypatch, report=_review_report([]), notes={}
    )
    _no_client(monkeypatch)

    assert _run(layout, report_path, notes_path) == 2
    assert "nothing in fr-CA.json is ready for a revision" in capsys.readouterr().err


def test_without_a_glossary_the_run_says_so(tmp_path, monkeypatch, capsys):
    layout, report_path, notes_path, _ = _tree(tmp_path, monkeypatch, glossary=None)
    client = FakeBedrock()
    _use_client(monkeypatch, client)

    assert _run(layout, report_path, notes_path) == 0

    assert "revising without a glossary" in capsys.readouterr().out
    assert "Glossary (English term" not in client.requests[0]["system"][0]["text"]
    assert _provenance(layout)["glossary"] is None


# --- end to end, with the real coverage worker -------------------------------------


@dataclass(frozen=True)
class TempCatalog(REVIEW.Layout):
    """The real checkout's worker and toolchain over a catalog directory of the test's own."""

    directory: Path = Path()

    @property
    def catalog_dir(self) -> Path:
        return self.directory / "i18n"

    @property
    def pricing(self) -> Path:
        return self.repo_root / "config_library" / "pricing.yaml"


def test_end_to_end_the_build_parity_check_decides_what_is_written(
    tmp_path, monkeypatch
):
    layout = TempCatalog(REPO_ROOT, directory=tmp_path)
    if not layout.parser_package.is_dir():
        if IN_CI:
            pytest.fail(
                f"{layout.parser_package} is missing; CI installs the UI's dependencies"
            )
        pytest.skip("the UI's node_modules are not installed (run make setup-node)")
    layout.catalog_dir.mkdir()
    layout.catalog("en").write_text(json.dumps(EN), encoding="utf-8")
    layout.catalog("fr-CA").write_text(
        json.dumps(_fr(), ensure_ascii=False), encoding="utf-8"
    )
    layout.glossary("fr-CA").write_text(GLOSSARY, encoding="utf-8")
    notes = tmp_path / "notes.json"
    notes.write_text(json.dumps({key: "" for key in EN}), encoding="utf-8")
    answers = {
        "nav.count": "{count,plural,one{# document}}",  # no other branch: does not parse
        "nav.empty": "'<Aucun contenu>'",  # ICU quoting kept: written
        "nav.greeting": "Bonjour",  # the argument is lost
        "nav.queue": "Ouvrir <b>la file d’attente de révision</b>",  # a tag the English lacks
        "nav.upload": "Téléverser des documents",  # as it was
    }
    _use_client(monkeypatch, FakeBedrock(answer_with(answers)))
    monkeypatch.setattr(REVIEW, "now", lambda: STARTED)

    provenance = tmp_path / "provenance.json"
    argv = [*RUN, "--notes", str(notes), "--provenance", str(provenance)]
    assert MODULE.main(argv, layout=layout) == 1

    record = json.loads(provenance.read_text(encoding="utf-8"))
    assert _outcomes(record) == {
        "nav.count": "refused",
        "nav.empty": "revised",
        "nav.greeting": "refused",
        "nav.queue": "refused",
        "nav.upload": "unchanged",
    }
    reasons = {entry["id"]: entry["reason"] for entry in record["entries"]}
    assert "missing argument(s) {name}" in reasons["nav.greeting"]
    assert "does not parse as ICU" in reasons["nav.count"]
    assert "rich-text tag(s) <b>" in reasons["nav.queue"]
    catalog = json.loads(layout.catalog("fr-CA").read_text(encoding="utf-8"))
    assert catalog["nav.empty"] == {
        "message": "'<Aucun contenu>'",
        "sourceHash": source_hash("'<No content>'"),
    }
    assert catalog["nav.greeting"]["message"] == "Salut {name}"
