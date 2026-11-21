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

    def __init__(self, reply=lambda text: text, formality: str | None = "FORMAL"):
        self.reply = reply
        self.formality = formality
        self.requests: list[dict[str, Any]] = []

    def translate_text(self, **request):
        self.requests.append(request)
        response: dict[str, Any] = {"TranslatedText": self.reply(request["Text"])}
        if self.formality:
            response["AppliedSettings"] = {"Formality": self.formality}
        return response


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
