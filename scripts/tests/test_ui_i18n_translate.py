# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pin what `scripts/ui_i18n_translate.py` sends, what it writes, and what it refuses.

The script behind `make ui-i18n-translate` calls Amazon Translate, so every test
here hands it a fake client: nothing needs credentials, a Region or the network,
and the fake records every request so the tests can say exactly what was sent.
The Node worker is replaced by canned answers where a test is about the driver's
own logic; the end-to-end tests run the real worker against a catalog in a
temporary directory when the UI's dependencies are installed, and in CI they
always are, so there they never skip.
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
    """Load `scripts/ui_i18n_translate.py` by path (scripts/ is not a package)."""
    path = REPO_ROOT / "scripts" / "ui_i18n_translate.py"
    spec = importlib.util.spec_from_file_location("_ui_i18n_translate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def source_hash(text: str) -> str:
    """The worker's ``sourceHash``, restated so the tests check its value, not just its shape."""
    digest = hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8"))
    return digest.hexdigest()[:16]


class FakeTranslate:
    """Stands in for a boto3 Translate client: records requests, answers through ``reply``."""

    def __init__(
        self,
        reply=lambda text: text,
        formality: str | None = "FORMAL",
        held_description: str | None = None,
        applied_terms: list[dict[str, str]] | None = None,
    ):
        self.reply = reply
        self.formality = formality
        self.held_description = held_description
        self.applied_terms = applied_terms
        self.requests: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []

    def translate_text(self, **request):
        self.requests.append(request)
        response: dict[str, Any] = {"TranslatedText": self.reply(request["Text"])}
        if self.formality and request["TargetLanguageCode"] != "en":
            response["AppliedSettings"] = {"Formality": self.formality}
        if self.applied_terms and request.get("TerminologyNames"):
            response["AppliedTerminologies"] = [
                {"Name": request["TerminologyNames"][0], "Terms": self.applied_terms}
            ]
        return response

    def get_terminology(self, **request):
        if self.held_description is None:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "none"}},
                "GetTerminology",
            )
        return {"TerminologyProperties": {"Description": self.held_description}}

    def import_terminology(self, **request):
        self.imports.append(request)
        return {}


def _use_client(monkeypatch, client) -> list[tuple[str, str | None]]:
    """Hand the module ``client`` instead of a boto3 one; returns the (region, profile) it was asked for."""
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


# --- the driver's own logic, with the worker replaced -------------------------------


def _plan(pending: list[dict[str, Any]], **extra) -> dict[str, Any]:
    plan = {
        "locale": "fr-CA",
        "sourceKeys": 10,
        "pending": pending,
        "deferred": 0,
        "unsplittable": [],
        "invalid": [],
        "problems": [],
        "dead": [],
    }
    plan.update(extra)
    return plan


def _entry(entry_id: str, *htmls: str, stale: bool = False) -> dict[str, Any]:
    return {
        "id": entry_id,
        "english": "".join(htmls),
        "sourceHash": "0123456789abcdef",
        "stale": stale,
        "sentences": [{"html": html, "translate": html != "#"} for html in htmls],
    }


def _canned_tree(tmp_path: Path, monkeypatch, plan: dict[str, Any], join=None):
    """A checkout whose worker answers ``plan`` and, for ``join``, echoes each message back."""
    layout = MODULE.Layout(tmp_path)
    layout.catalog_dir.mkdir(parents=True)
    layout.parser_package.mkdir(parents=True)
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command[2:])
        if command[2] == "plan":
            stdout = json.dumps(plan)
        else:
            request = json.loads(kwargs["input"])
            results = (
                join(request)
                if join
                else [
                    {"id": e["id"], "message": "".join(e["answers"])}
                    for e in request["entries"]
                ]
            )
            stdout = json.dumps({"results": results})
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(MODULE, "run", run)
    return layout, calls


def test_a_dry_run_prints_the_plan_and_the_estimate_and_calls_nothing(
    tmp_path, monkeypatch, capsys
):
    plan = _plan(
        [_entry("a.one", "Upload documents"), _entry("a.two", "Hello", stale=True)]
    )
    layout, calls = _canned_tree(tmp_path, monkeypatch, plan)
    _no_client(monkeypatch)

    assert MODULE.main(["--locale", "fr-CA", "--dry-run"], layout=layout) == 0

    out = capsys.readouterr().out
    assert (
        "2 entries to translate (1 missing, 1 stale) of 10 in en.json; 21 characters"
        in out
    )
    assert calls == [["plan", str(layout.catalog_dir), "fr-CA"]]
    assert not layout.catalog("fr-CA").exists()


def test_the_run_refuses_to_start_over_the_character_cap(tmp_path, monkeypatch, capsys):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([_entry("a.one", "x" * 50)]))
    _no_client(monkeypatch)

    argv = ["--locale", "fr-CA", "--region", "ca-central-1", "--max-chars", "49"]
    assert MODULE.main(argv, layout=layout) == 2

    assert "50 characters is over --max-chars 49" in capsys.readouterr().err
    assert not layout.catalog("fr-CA").exists()


def test_the_default_cap_is_about_five_dollars():
    assert MODULE.DEFAULT_MAX_CHARS * MODULE.PRICE_PER_CHARACTER_USD == pytest.approx(
        5, abs=0.01
    )


def test_limit_is_passed_to_the_worker(tmp_path, monkeypatch):
    layout, calls = _canned_tree(tmp_path, monkeypatch, _plan([], deferred=3))
    _no_client(monkeypatch)

    assert (
        MODULE.main(["--locale", "fr-CA", "--dry-run", "--limit", "2"], layout=layout)
        == 0
    )
    assert calls == [["plan", str(layout.catalog_dir), "fr-CA", "--limit", "2"]]


@pytest.mark.parametrize(
    "argv",
    [
        ["--locale", "fr-CA"],
        ["--locale", "fr-CA", "--dry-run", "--limit", "0"],
    ],
    ids=["no region outside a dry run", "a limit below one"],
)
def test_usage_errors_exit_2(argv, tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        MODULE.main(argv, layout=MODULE.Layout(tmp_path))
    assert exit_info.value.code == 2


def test_a_missing_toolchain_is_a_tool_error(tmp_path, capsys):
    assert (
        MODULE.main(["--locale", "fr-CA", "--dry-run"], layout=MODULE.Layout(tmp_path))
        == 2
    )
    assert "run make setup-node first" in capsys.readouterr().err


def test_it_sends_the_formal_register_and_only_sentences_with_text(
    tmp_path, monkeypatch
):
    plan = _plan([_entry("a.count", "#", "Hello")])
    layout, _ = _canned_tree(tmp_path, monkeypatch, plan)
    client = FakeTranslate(reply=lambda text: "Bonjour")
    asked = _use_client(monkeypatch, client)

    argv = ["--locale", "fr-CA", "--region", "ca-central-1", "--profile", "dev"]
    assert MODULE.main(argv, layout=layout) == 0

    assert asked == [("ca-central-1", "dev")]
    assert client.requests == [
        {
            "Text": "Hello",
            "SourceLanguageCode": "en",
            "TargetLanguageCode": "fr-CA",
            "Settings": {"Formality": "FORMAL"},
        }
    ]
    written = json.loads(layout.catalog("fr-CA").read_text(encoding="utf-8"))
    assert written == {
        "a.count": {"message": "#Bonjour", "sourceHash": "0123456789abcdef"}
    }


def test_an_answer_without_the_formal_register_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")]))
    _use_client(monkeypatch, FakeTranslate(formality=None))

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 2
    )

    assert "without the FORMAL register" in capsys.readouterr().err
    assert not layout.catalog("fr-CA").exists()


def test_an_aws_error_writes_nothing(tmp_path, monkeypatch, capsys):
    layout, calls = _canned_tree(
        tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")])
    )

    class Denied(FakeTranslate):
        def translate_text(self, **request):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "TranslateText",
            )

    _use_client(monkeypatch, Denied())

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 2
    )

    assert (
        "Translate refused the run, so nothing was written" in capsys.readouterr().err
    )
    assert [call[0] for call in calls] == ["plan"]
    assert not layout.catalog("fr-CA").exists()


def test_refused_entries_are_left_out_and_named_and_the_rest_written(
    tmp_path, monkeypatch, capsys
):
    plan = _plan([_entry("a.good", "Hello"), _entry("a.bad", "Bye")])
    existing = {"a.kept": {"message": "Gardé", "sourceHash": "fedcba9876543210"}}

    def join(request):
        return [
            {"id": "a.good", "message": "Bonjour"},
            {"id": "a.bad", "errors": ["a.bad: the answer lost span(s) 0"]},
        ]

    layout, _ = _canned_tree(tmp_path, monkeypatch, plan, join=join)
    layout.catalog("fr-CA").write_text(json.dumps(existing), encoding="utf-8")
    _use_client(monkeypatch, FakeTranslate())

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 1
    )

    written = json.loads(layout.catalog("fr-CA").read_text(encoding="utf-8"))
    assert written == {
        "a.good": {"message": "Bonjour", "sourceHash": "0123456789abcdef"},
        "a.kept": existing["a.kept"],
    }
    assert (
        "left out (English shown): a.bad: the answer lost span(s) 0"
        in capsys.readouterr().out
    )


def test_the_catalog_is_written_sorted_with_two_spaces_and_a_final_newline(tmp_path):
    path = tmp_path / "fr-CA.json"
    MODULE.write_catalog(
        path,
        {
            "b": {"sourceHash": "1", "message": "É"},
            "a": {"message": "x", "sourceHash": "2"},
        },
    )
    assert path.read_text(encoding="utf-8") == (
        '{\n  "a": {\n    "message": "x",\n    "sourceHash": "2"\n  },\n'
        '  "b": {\n    "message": "É",\n    "sourceHash": "1"\n  }\n}\n'
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fr-CA.json"]


def test_an_unknown_profile_is_a_tool_error_not_a_traceback(
    tmp_path, monkeypatch, capsys
):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")]))

    def make_client(region, profile):
        raise ProfileNotFound(profile=profile)

    monkeypatch.setattr(MODULE, "make_client", make_client)

    argv = ["--locale", "fr-CA", "--region", "ca-central-1", "--profile", "nope"]
    assert MODULE.main(argv, layout=layout) == 2

    assert "The config profile (nope) could not be found" in capsys.readouterr().err
    assert not layout.catalog("fr-CA").exists()


def test_hand_translations_and_problems_are_listed_and_left_alone(
    tmp_path, monkeypatch, capsys
):
    plan = _plan(
        [],
        unsplittable=[
            {"id": "a.tag", "reason": "Cannot hoist plural/select within a tag element"}
        ],
        problems=["fr-CA.json: a.bad: the translation is missing argument(s) {name}"],
        dead=["a.gone"],
    )
    layout, _ = _canned_tree(tmp_path, monkeypatch, plan)
    _no_client(monkeypatch)

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 1
    )

    out = capsys.readouterr().out
    assert "needs a hand translation: a.tag (Cannot hoist" in out
    assert "left as it is, invalid: fr-CA.json: a.bad" in out
    assert "left as it is, dead (not in en.json): a.gone" in out


# --- end to end, with the real worker --------------------------------------------


@dataclass(frozen=True)
class TempCatalog(MODULE.Layout):
    """The real checkout's worker and toolchain over a catalog directory of the test's own."""

    directory: Path = Path()

    @property
    def catalog_dir(self) -> Path:
        return self.directory

    @property
    def provenance_dir(self) -> Path:
        return self.directory / "provenance"


EN = {
    "nav.upload": {"defaultMessage": "Upload documents", "description": "Button"},
    "nav.greeting": {
        "defaultMessage": "Hello {name}: welcome",
        "description": "Greeting",
    },
    "nav.count": {
        "defaultMessage": "{count, plural, one {# document} other {# documents}}",
        "description": "Count",
    },
    "nav.guide": {
        "defaultMessage": "Read the <link>guide</link>",
        "description": "Link",
    },
    "nav.done": {"defaultMessage": "Done", "description": "Status"},
}

FRENCH = {
    "Upload documents": "Téléverser des documents",
    "Hello ": "Bonjour ",
    ": welcome": " : bienvenue",
    " document": " document",
    " documents": " documents",
    "Read the ": "Lisez le ",
    ">guide<": ">guide<",
}


def _fake_french(html: str) -> str:
    for english, french in FRENCH.items():
        html = html.replace(english, french)
    return html


def _real_tree(tmp_path: Path) -> TempCatalog:
    layout = TempCatalog(REPO_ROOT, directory=tmp_path)
    if not layout.parser_package.is_dir():
        if IN_CI:
            pytest.fail(
                f"{layout.parser_package} is missing; CI installs the UI's dependencies"
            )
        pytest.skip("the UI's node_modules are not installed (run make setup-node)")
    (tmp_path / "en.json").write_text(json.dumps(EN), encoding="utf-8")
    return layout


def _check_catalog(layout: TempCatalog, locale: str) -> subprocess.CompletedProcess:
    worker = layout.ui_root / "scripts" / "i18n" / "check-catalog.mjs"
    return subprocess.run(
        ["node", str(worker), locale, "--catalog-dir", str(layout.catalog_dir)],
        cwd=layout.ui_root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_end_to_end_only_missing_and_stale_entries_are_sent_and_the_catalog_compiles(
    tmp_path, monkeypatch
):
    layout = _real_tree(tmp_path)
    current = {"message": "Terminé", "sourceHash": source_hash("Done")}
    layout.catalog("fr-CA").write_text(
        json.dumps(
            {
                "nav.done": current,
                "nav.upload": {"message": "Ancienne", "sourceHash": "0123456789abcdef"},
            }
        ),
        encoding="utf-8",
    )
    client = FakeTranslate(reply=_fake_french)
    _use_client(monkeypatch, client)

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 0
    )

    written = json.loads(layout.catalog("fr-CA").read_text(encoding="utf-8"))
    assert written["nav.done"] == current
    assert {entry_id: entry["message"] for entry_id, entry in written.items()} == {
        "nav.count": "{count,plural,one{# document} other{# documents}}",
        "nav.done": "Terminé",
        "nav.greeting": "Bonjour {name}\u00a0: bienvenue",
        "nav.guide": "Lisez le <link>guide</link>",
        "nav.upload": "Téléverser des documents",
    }
    for entry_id, entry in written.items():
        assert entry["sourceHash"] == source_hash(EN[entry_id]["defaultMessage"])
    sent = [request["Text"] for request in client.requests]
    assert "Done" not in sent
    assert len(sent) == 5
    check = _check_catalog(layout, "fr-CA")
    assert check.returncode == 0, check.stdout + check.stderr


def test_end_to_end_a_broken_answer_is_left_out_and_the_catalog_still_compiles(
    tmp_path, monkeypatch, capsys
):
    layout = _real_tree(tmp_path)

    def drop_spans(html: str) -> str:
        return (
            "Bonjour" if "data-ph" in html and "Hello" in html else _fake_french(html)
        )

    _use_client(monkeypatch, FakeTranslate(reply=drop_spans))

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 1
    )

    written = json.loads(layout.catalog("fr-CA").read_text(encoding="utf-8"))
    assert "nav.greeting" not in written
    assert "nav.greeting: the answer lost span(s) 0" in capsys.readouterr().out
    check = _check_catalog(layout, "fr-CA")
    assert check.returncode == 0, check.stdout + check.stderr


# --- the glossary, the provenance file and the back-translation ----------------------

GLOSSARY = (
    "en,fr-CA,note\n"
    "Amazon Bedrock,Amazon Bedrock,Product name.\n"
    'review queue,file d’attente de révision,"Noun phrase, never a verb."\n'
    "settings,paramètres,Noun.\n"
)
TERMINOLOGY_DATA = (
    "en,fr-CA\n"
    "Amazon Bedrock,Amazon Bedrock\n"
    "review queue,file d’attente de révision\n"
    "settings,paramètres\n"
).encode("utf-8")
TERMINOLOGY_SHA = hashlib.sha256(TERMINOLOGY_DATA).hexdigest()


def _with_glossary(layout, text: str = GLOSSARY) -> None:
    layout.glossary("fr-CA").write_text(text, encoding="utf-8")


def test_the_glossary_is_imported_without_its_note_and_named_in_every_request(
    tmp_path, monkeypatch, capsys
):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")]))
    _with_glossary(layout)
    client = FakeTranslate()
    _use_client(monkeypatch, client)

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 0
    )

    assert client.imports == [
        {
            "Name": "idp-ui-fr-CA",
            "MergeStrategy": "OVERWRITE",
            "Description": f"sha256:{TERMINOLOGY_SHA}",
            "TerminologyData": {
                "File": TERMINOLOGY_DATA,
                "Format": "CSV",
                "Directionality": "UNI",
            },
        }
    ]
    assert [request["TerminologyNames"] for request in client.requests] == [
        ["idp-ui-fr-CA"]
    ]
    assert f"terminology idp-ui-fr-CA imported (sha256 {TERMINOLOGY_SHA[:12]})" in (
        capsys.readouterr().out
    )


def test_an_unchanged_glossary_is_not_imported_again(tmp_path, monkeypatch):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")]))
    _with_glossary(layout)
    client = FakeTranslate(held_description=f"sha256:{TERMINOLOGY_SHA}")
    _use_client(monkeypatch, client)

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 0
    )
    assert client.imports == []


def test_a_changed_glossary_is_imported_again(tmp_path, monkeypatch):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")]))
    _with_glossary(layout)
    client = FakeTranslate(held_description="sha256:" + "0" * 64)
    _use_client(monkeypatch, client)

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 0
    )
    assert len(client.imports) == 1


@pytest.mark.parametrize(
    "text, reason",
    [
        (
            "en,fr,note\nsettings,paramètres,Noun.\n",
            "the header must start with en,fr-CA",
        ),
        (
            "en,fr-CA,note\nsettings,,Noun.\n",
            "glossary.fr-CA.csv:2: a term needs its English",
        ),
    ],
    ids=["another locale's header", "an empty target"],
)
def test_a_malformed_glossary_is_a_tool_error(
    tmp_path, monkeypatch, capsys, text, reason
):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")]))
    _with_glossary(layout, text)
    _no_client(monkeypatch)

    assert MODULE.main(["--locale", "fr-CA", "--dry-run"], layout=layout) == 2
    assert reason in capsys.readouterr().err


def test_without_a_glossary_the_run_names_no_terminology(tmp_path, monkeypatch, capsys):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")]))
    client = FakeTranslate()
    _use_client(monkeypatch, client)

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 0
    )
    assert "TerminologyNames" not in client.requests[0]
    assert "running without a terminology" in capsys.readouterr().out


def test_multi_word_terms_keep_the_glossary_case():
    terminology = MODULE.Terminology(
        name="idp-ui-fr-CA",
        pairs=(
            ("review queue", "file d’attente de révision"),
            ("settings", "paramètres"),
        ),
        source=Path("glossary.fr-CA.csv"),
    )
    keep = MODULE.keep_glossary_case
    assert keep("Ouvrez la File D’Attente De Révision", terminology) == (
        "Ouvrez la File d’attente de révision"
    )
    assert keep("dans la file d’attente de révision", terminology) == (
        "dans la file d’attente de révision"
    )
    assert (
        keep("FILE D’ATTENTE DE RÉVISION", terminology) == "FILE D’ATTENTE DE RÉVISION"
    )
    assert keep("Paramètres Avancés", terminology) == "Paramètres Avancés"
    assert keep("Ouvrez la File D’Attente De Révision", None) == (
        "Ouvrez la File D’Attente De Révision"
    )


def test_the_written_message_keeps_the_glossary_case(tmp_path, monkeypatch):
    def join(request):
        return [{"id": "a.one", "message": "File D’Attente De Révision {name}"}]

    layout, _ = _canned_tree(
        tmp_path, monkeypatch, _plan([_entry("a.one", "Review Queue")]), join=join
    )
    _with_glossary(layout)
    _use_client(monkeypatch, FakeTranslate())

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 0
    )
    written = json.loads(layout.catalog("fr-CA").read_text(encoding="utf-8"))
    assert written["a.one"]["message"] == "File d’attente de révision {name}"


def test_the_provenance_file_records_the_run_outside_the_catalog(tmp_path, monkeypatch):
    plan = _plan([_entry("a.one", "Open the review queue")])
    layout, _ = _canned_tree(tmp_path, monkeypatch, plan)
    _with_glossary(layout)
    terms = [{"SourceText": "review queue", "TargetText": "file d’attente de révision"}]
    _use_client(
        monkeypatch,
        FakeTranslate(reply=lambda text: "Ouvrir la file", applied_terms=terms),
    )
    monkeypatch.setattr(
        MODULE,
        "now",
        lambda: MODULE.datetime(2030, 1, 2, 3, 4, 5, tzinfo=MODULE.timezone.utc),
    )

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 0
    )

    path = layout.provenance_dir / "fr-CA-20300102T030405Z.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert {key: record[key] for key in record if key != "entries"} == {
        "engine": "Amazon Translate",
        "operation": "TranslateText",
        "region": "ca-central-1",
        "sourceLanguage": "en",
        "targetLanguage": "fr-CA",
        "formality": "FORMAL",
        "terminology": {
            "name": "idp-ui-fr-CA",
            "sha256": TERMINOLOGY_SHA,
            "glossary": "src/ui/src/i18n/glossary.fr-CA.csv",
            "terms": 3,
        },
        "date": "2030-01-02T03:04:05Z",
        "characters": {"translated": 21, "backTranslated": 0},
        "backTranslationError": None,
    }
    assert record["entries"] == [
        {
            "id": "a.one",
            "english": "Open the review queue",
            "stale": False,
            "sentences": [
                {
                    "sent": "Open the review queue",
                    "answer": "Ouvrir la file",
                    "appliedTerms": [
                        {
                            "source": "review queue",
                            "target": "file d’attente de révision",
                        }
                    ],
                }
            ],
            "message": "Ouvrir la file",
        }
    ]
    catalog = json.loads(layout.catalog("fr-CA").read_text(encoding="utf-8"))
    assert catalog == {
        "a.one": {"message": "Ouvrir la file", "sourceHash": "0123456789abcdef"}
    }


def test_back_translation_goes_to_the_provenance_without_register_or_terminology(
    tmp_path, monkeypatch
):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")]))
    _with_glossary(layout)
    client = FakeTranslate(
        reply=lambda text: "Bonjour" if text == "Hello" else "Good day"
    )
    _use_client(monkeypatch, client)
    provenance = tmp_path / "run.json"

    argv = ["--locale", "fr-CA", "--region", "ca-central-1", "--back-translate"]
    assert MODULE.main([*argv, "--provenance", str(provenance)], layout=layout) == 0

    assert client.requests[1] == {
        "Text": "Bonjour",
        "SourceLanguageCode": "fr-CA",
        "TargetLanguageCode": "en",
    }
    record = json.loads(provenance.read_text(encoding="utf-8"))
    assert record["entries"][0]["sentences"][0]["backTranslation"] == "Good day"
    assert record["characters"] == {"translated": 5, "backTranslated": 7}


def test_back_translation_counts_against_the_cap_before_and_during_the_run(
    tmp_path, monkeypatch, capsys
):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")]))
    long_answer = "Bonjour à toutes et à tous"
    client = FakeTranslate(reply=lambda text: long_answer if text == "Hello" else "Hi")
    _use_client(monkeypatch, client)
    provenance = tmp_path / "run.json"
    argv = ["--locale", "fr-CA", "--region", "ca-central-1", "--back-translate"]

    # Hello is 5 characters; the estimate adds 30 percent more for the answer: 5 + 6.
    assert MODULE.main([*argv, "--max-chars", "10"], layout=layout) == 2
    assert "11 characters is over --max-chars 10" in capsys.readouterr().err

    # The run starts, but the real answer is longer than estimated: it is not sent back.
    assert (
        MODULE.main(
            [*argv, "--max-chars", "11", "--provenance", str(provenance)], layout=layout
        )
        == 0
    )
    assert len(client.requests) == 1
    record = json.loads(provenance.read_text(encoding="utf-8"))
    assert record["entries"][0]["sentences"][0]["backTranslation"] is None


def test_a_failed_back_translation_keeps_the_paid_for_answers(
    tmp_path, monkeypatch, capsys
):
    plan = _plan([_entry("a.one", "Hello"), _entry("a.two", "Bye")])
    layout, _ = _canned_tree(tmp_path, monkeypatch, plan)

    class Throttled(FakeTranslate):
        def translate_text(self, **request):
            if request["TargetLanguageCode"] == "en":
                raise ClientError(
                    {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                    "TranslateText",
                )
            return super().translate_text(**request)

    _use_client(monkeypatch, Throttled(reply=lambda text: "Bonjour"))
    provenance = tmp_path / "run.json"
    argv = ["--locale", "fr-CA", "--region", "ca-central-1", "--back-translate"]

    assert MODULE.main([*argv, "--provenance", str(provenance)], layout=layout) == 1

    written = json.loads(layout.catalog("fr-CA").read_text(encoding="utf-8"))
    assert set(written) == {"a.one", "a.two"}
    record = json.loads(provenance.read_text(encoding="utf-8"))
    assert "ThrottlingException" in record["backTranslationError"]
    assert record["characters"]["backTranslated"] == 0
    assert "the back-translation stopped on an AWS error" in capsys.readouterr().out


def test_a_provenance_path_under_src_is_refused_before_any_call(
    tmp_path, monkeypatch, capsys
):
    layout, calls = _canned_tree(
        tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")])
    )
    _no_client(monkeypatch)
    inside = layout.catalog_dir / "provenance.json"

    argv = [
        "--locale",
        "fr-CA",
        "--region",
        "ca-central-1",
        "--provenance",
        str(inside),
    ]
    assert MODULE.main(argv, layout=layout) == 2

    assert "is under src/, which ships" in capsys.readouterr().err
    assert calls == []


def test_a_provenance_file_that_cannot_be_written_leaves_the_catalog_alone(
    tmp_path, monkeypatch, capsys
):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")]))
    _use_client(monkeypatch, FakeTranslate())
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")

    argv = ["--locale", "fr-CA", "--region", "ca-central-1"]
    assert (
        MODULE.main([*argv, "--provenance", str(blocker / "run.json")], layout=layout)
        == 2
    )

    assert "the provenance file could not be written, so the catalog was not" in (
        capsys.readouterr().err
    )
    assert not layout.catalog("fr-CA").exists()


def test_a_terminology_error_other_than_not_found_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([_entry("a.one", "Hello")]))
    _with_glossary(layout)

    class NoAccess(FakeTranslate):
        def get_terminology(self, **request):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "GetTerminology",
            )

    client = NoAccess()
    _use_client(monkeypatch, client)

    assert (
        MODULE.main(["--locale", "fr-CA", "--region", "ca-central-1"], layout=layout)
        == 2
    )

    assert "AccessDeniedException" in capsys.readouterr().err
    assert client.imports == [] and client.requests == []
    assert not layout.catalog("fr-CA").exists()


def test_an_empty_glossary_is_a_tool_error_and_a_byte_order_mark_is_not(
    tmp_path, monkeypatch, capsys
):
    layout, _ = _canned_tree(tmp_path, monkeypatch, _plan([]))
    _no_client(monkeypatch)

    _with_glossary(layout, "en,fr-CA,note\n")
    assert MODULE.main(["--locale", "fr-CA", "--dry-run"], layout=layout) == 2
    assert "the glossary has no terms" in capsys.readouterr().err

    _with_glossary(layout, "\ufeff" + GLOSSARY)
    assert MODULE.main(["--locale", "fr-CA", "--dry-run"], layout=layout) == 0


def test_terms_match_whole_words_either_apostrophe_and_the_longest_term_first():
    terminology = MODULE.Terminology(
        name="idp-ui-fr-CA",
        pairs=(
            ("inbox", "boîte de réception"),
            ("reviewer inbox", "boîte de réception du réviseur"),
            ("queue", "file d’attente"),
            ("review studio documents", "documents du Studio de révision"),
            ("Review Studio", "Studio de révision"),
        ),
        source=Path("glossary.fr-CA.csv"),
    )
    keep = MODULE.keep_glossary_case
    # Inside a longer word: left alone.
    assert keep("Le Profile D’Attente", terminology) == "Le Profile D’Attente"
    # Translate's straight apostrophe is matched too, and the glossary's spelling restored.
    assert keep("La File D'Attente", terminology) == "La File d’attente"
    # A longer term wins over the shorter one inside it, and is rewritten once.
    assert keep("Boîte De Réception Du Réviseur", terminology) == (
        "Boîte de réception du réviseur"
    )
    assert keep("les Documents Du Studio De Révision", terminology) == (
        "les Documents du Studio de révision"
    )
    assert (
        keep("dans le studio de révision", terminology) == "dans le studio de révision"
    )
