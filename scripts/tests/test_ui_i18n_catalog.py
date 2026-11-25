# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ui_i18n_catalog.py: the todo list, the ingest and the drop of stale entries.

The tests that list or write run the real coverage worker, whose parity check is the UI
build's, over a catalog directory of their own, so they need the UI's node_modules,
which CI always installs. The drop, the locale guard and the missing toolchain need no
node.
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

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Same rule as ``IN_CI`` in ``test_ui_i18n_revise.py``.
IN_CI = bool(os.getenv("CI") or os.getenv("GITLAB_CI") or os.getenv("GITHUB_ACTIONS"))


def _load_module():
    """Load `scripts/ui_i18n_catalog.py` by path (scripts/ is not a package)."""
    path = REPO_ROOT / "scripts" / "ui_i18n_catalog.py"
    spec = importlib.util.spec_from_file_location("_ui_i18n_catalog", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()
REVIEW = MODULE.review

NBSP = "\u00a0"


def source_hash(text: str) -> str:
    """The catalog's ``sourceHash``, restated so the test catalogs are well formed."""
    digest = hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8"))
    return digest.hexdigest()[:16]


EN = {
    "nav.confirm": {
        "defaultMessage": 'Type "delete" to confirm',
        "description": "Help under the field of the delete dialog",
    },
    "nav.count": {
        "defaultMessage": "{count, plural, one {# document} other {# documents}}",
        "description": "Count of documents in the list. {count} is the number.",
    },
    "nav.greeting": {
        "defaultMessage": "Hello {name}",
        "description": "Greeting on the home page. {name} is the user's name.",
    },
    "nav.help": {
        "defaultMessage": "Read the <link>guide</link>",
        "description": "Link to the user guide",
    },
    "nav.mode": {
        "defaultMessage": "{mode, select, fast {Fast} other {Careful}}",
        "description": "The processing mode",
    },
    "nav.name": {"defaultMessage": "Name: {name}", "description": "The user's name"},
    "nav.queue": {
        "defaultMessage": "Open the Review Queue",
        "description": "Link to the review queue",
    },
    "nav.sure": {"defaultMessage": "Are you sure?", "description": "Confirmation"},
    "nav.upload": {
        "defaultMessage": "Upload a document",
        "description": "Button that opens the upload page",
    },
}

GLOSSARY = (
    "en,fr-CA,note\n"
    "document,document,Noun.\n"
    "review queue,file d’attente de révision,Noun phrase.\n"
)


def _entry(message: str, english: str) -> dict[str, str]:
    return {"message": message, "sourceHash": source_hash(english)}


def _catalog() -> dict[str, Any]:
    """A catalog in every state the tools tell apart: ``nav.queue`` is current;
    ``nav.greeting`` is stale and still compiles; ``nav.count`` is stale and no longer
    compiles, since its English gained a plural (the coverage worker lists it as invalid);
    ``nav.gone`` is dead; and the other ids of ``EN`` are missing."""
    return {
        "nav.count": _entry("Documents", "Documents"),
        "nav.gone": _entry("Parti", "Gone"),
        "nav.greeting": _entry("Salut {name}", "Hi {name}"),
        "nav.queue": _entry(
            "Ouvrir la file d’attente de révision", "Open the Review Queue"
        ),
    }


#: What the agent writes for each entry the todo list holds.
FRENCH = {
    "nav.confirm": "Tapez « supprimer » pour confirmer",
    "nav.count": "{count, plural, one {# document} other {# documents}}",
    "nav.greeting": "Bonjour {name}",
    "nav.help": "Lire le <link>guide</link>",
    "nav.mode": "{mode, select, fast {Rapide} other {Prudent}}",
    "nav.name": "Nom : {name}",
    "nav.sure": "Êtes-vous sûr ?",
    "nav.upload": "Téléverser un document",
}


@dataclass(frozen=True)
class TempCatalog(REVIEW.Layout):
    """The real checkout's worker and toolchain over a catalog directory of the test's own."""

    directory: Path = Path()

    @property
    def catalog_dir(self) -> Path:
        return self.directory / "i18n"


def _write(layout, catalog: Any, *, en=EN, glossary: str | None = GLOSSARY) -> None:
    layout.catalog_dir.mkdir(parents=True, exist_ok=True)
    layout.catalog("en").write_text(json.dumps(en), encoding="utf-8")
    layout.catalog("fr-CA").write_text(
        json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
    )
    if glossary is not None:
        layout.glossary("fr-CA").write_text(glossary, encoding="utf-8")


def _real_tree(tmp_path: Path, catalog: Any = None, **kwargs) -> TempCatalog:
    layout = TempCatalog(REPO_ROOT, directory=tmp_path)
    if not layout.parser_package.is_dir():
        if IN_CI:
            pytest.fail(
                f"{layout.parser_package} is missing; CI installs the UI's dependencies"
            )
        pytest.skip("the UI's node_modules are not installed (run make setup-node)")
    _write(layout, _catalog() if catalog is None else catalog, **kwargs)
    return layout


def _plain_tree(tmp_path: Path, catalog: Any = None) -> TempCatalog:
    """A tree whose toolchain is absent: the repository root is the test's own."""
    layout = TempCatalog(tmp_path, directory=tmp_path)
    _write(layout, _catalog() if catalog is None else catalog)
    return layout


def _read(layout) -> dict[str, Any]:
    return json.loads(layout.catalog("fr-CA").read_text(encoding="utf-8"))


def _coverage(layout) -> dict[str, Any]:
    report = REVIEW.call_worker(
        layout, [str(layout.catalog_dir), "--json", "--locale", "fr-CA"]
    )
    return report["locales"]["fr-CA"]


def _todo(layout, capsys) -> dict[str, Any]:
    assert MODULE.main(["todo", "--locale", "fr-CA"], layout=layout) == 0
    return json.loads(capsys.readouterr().out)


def _list_file(tmp_path: Path, entries: list[dict[str, Any]], locale="fr-CA") -> Path:
    path = tmp_path / "todo.json"
    path.write_text(
        json.dumps({"locale": locale, "entries": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _ingest(layout, path: Path) -> int:
    return MODULE.main(["ingest", "--locale", "fr-CA", str(path)], layout=layout)


# --- todo ---------------------------------------------------------------------------


def test_todo_lists_the_missing_and_stale_entries_with_what_a_translator_needs(
    tmp_path, capsys
):
    layout = _real_tree(tmp_path)
    assert MODULE.main(["todo", "--locale", "fr-CA"], layout=layout) == 0
    out, err = capsys.readouterr()

    listed = json.loads(out)  # stdout is the JSON and nothing else
    assert listed["locale"] == "fr-CA"
    entries = {entry["id"]: entry for entry in listed["entries"]}
    assert list(entries) == sorted(FRENCH), "missing and stale, in id order"
    assert entries["nav.upload"] == {
        "id": "nav.upload",
        "english": "Upload a document",
        "description": "Button that opens the upload page",
        "glossaryTerms": [{"en": "document", "fr-CA": "document", "note": "Noun."}],
    }
    assert entries["nav.greeting"]["stale"] is True
    assert entries["nav.greeting"]["translation"] == "Salut {name}"
    # Stale and no longer compiling: the coverage worker calls it invalid, todo lists it.
    assert _coverage(layout)["invalid"] == ["nav.count"]
    assert entries["nav.count"]["stale"] is True
    assert entries["nav.count"]["translation"] == "Documents"
    assert "stale" not in entries["nav.help"]
    assert "8 entries to translate in " in err
    assert ", 2 of them stale: " in err
    assert "make ui-i18n-ingest LOCALE=fr-CA FILE=<file>" in err
    assert "make ui-i18n-drop-stale LOCALE=fr-CA" in err


def test_todo_leaves_out_a_current_entry_a_dead_one_and_a_broken_current_one(
    tmp_path, capsys
):
    """A translation that fails the parity check for the English it records is not stale:
    nobody changed the English, so fixing it is a person's call, as the gate says."""
    catalog = _catalog()
    catalog["nav.mode"] = _entry(
        "{mode, select, fast {Rapide}}", EN["nav.mode"]["defaultMessage"]
    )
    layout = _real_tree(tmp_path, catalog)
    ids = [entry["id"] for entry in _todo(layout, capsys)["entries"]]
    assert "nav.mode" not in ids
    assert "nav.queue" not in ids and "nav.gone" not in ids


def test_todo_with_nothing_to_translate_prints_an_empty_list(tmp_path, capsys):
    catalog = {
        entry_id: _entry(
            FRENCH.get(entry_id, "Ouvrir la file"), source["defaultMessage"]
        )
        for entry_id, source in EN.items()
    }
    layout = _real_tree(tmp_path, catalog)
    assert MODULE.main(["todo", "--locale", "fr-CA"], layout=layout) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"locale": "fr-CA", "entries": []}
    assert "nothing to translate in " in err
    assert "make ui-i18n-" not in err


def test_todo_without_a_glossary_lists_no_terms(tmp_path, capsys):
    layout = _real_tree(tmp_path, glossary=None)
    entries = _todo(layout, capsys)["entries"]
    assert entries and all(entry["glossaryTerms"] == [] for entry in entries)


# --- ingest -------------------------------------------------------------------------


def test_todo_then_ingest_leaves_every_entry_translated_and_the_gate_passing(
    tmp_path, capsys
):
    layout = _real_tree(tmp_path)
    listed = _todo(layout, capsys)
    for entry in listed["entries"]:
        entry["message"] = FRENCH[entry["id"]]
    path = _list_file(tmp_path, listed["entries"])

    assert _ingest(layout, path) == 0
    out = capsys.readouterr().out
    assert "8 written, 0 unchanged, 0 refused of 8" in out
    assert "dead entries removed: 1" in out
    assert "still to translate" not in out

    coverage = _coverage(layout)
    assert coverage["translated"] == len(EN)
    assert (coverage["missing"], coverage["stale"], coverage["invalid"]) == ([], [], [])
    assert coverage["dead"] == []
    catalog = _read(layout)
    assert MODULE.stale_ids(EN, catalog) == [], "the gate's stale check passes"
    compiled = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "node",
            str(layout.ui_root / "scripts" / "i18n" / "check-catalog.mjs"),
            "fr-CA",
            "--catalog-dir",
            str(layout.catalog_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    assert catalog["nav.greeting"] == _entry("Bonjour {name}", "Hello {name}")
    assert catalog["nav.count"]["sourceHash"] == source_hash(
        EN["nav.count"]["defaultMessage"]
    )
    assert list(
        json.loads(layout.catalog("fr-CA").read_text(encoding="utf-8"))
    ) == sorted(catalog), "written in id order"


@pytest.mark.parametrize(
    ("entry_id", "message", "written"),
    [
        ("nav.name", "Nom : {name}", f"Nom{NBSP}: {{name}}"),
        ("nav.sure", "Êtes-vous sûr ?", "Êtes-vous sûr?"),
        (
            "nav.confirm",
            "Tapez « supprimer » pour confirmer",
            f"Tapez «{NBSP}supprimer{NBSP}» pour confirmer",
        ),
    ],
)
def test_ingest_gives_fr_ca_its_canadian_spacing(tmp_path, entry_id, message, written):
    layout = _real_tree(tmp_path)
    path = _list_file(tmp_path, [{"id": entry_id, "message": message}])
    assert _ingest(layout, path) == 0
    assert _read(layout)[entry_id]["message"] == written


@pytest.mark.parametrize(
    ("entry_id", "message", "reason"),
    [
        ("nav.greeting", "Bonjour", "is missing argument(s) {name}"),
        (
            "nav.greeting",
            "Bonjour {name} {city}",
            "has argument(s) {city} that the English does not",
        ),
        (
            "nav.count",
            "{count, select, one {# document} other {# documents}}",
            "argument {count} is a plural in the English and a select in the translation",
        ),
        (
            "nav.mode",
            "{mode, plural, one {Rapide} other {Prudent}}",
            "argument {mode} is a select in the English and a plural in the translation",
        ),
        ("nav.count", "{count, plural, one {# document}}", "does not parse as ICU"),
        ("nav.help", "Lire le guide", "is missing rich-text tag(s) <link>"),
        (
            "nav.help",
            "Lire le <link>guide</link> <b>ici</b>",
            "has rich-text tag(s) <b>",
        ),
        ("nav.help", "Lire le <link>guide", "does not parse as ICU"),
    ],
)
def test_a_message_that_breaks_parity_is_refused_with_its_reason_and_changes_nothing(
    tmp_path, capsys, entry_id, message, reason
):
    catalog = _catalog()
    del catalog["nav.gone"]  # nothing dead, so nothing else would change the file
    layout = _real_tree(tmp_path, catalog)
    before = layout.catalog("fr-CA").read_bytes()
    path = _list_file(tmp_path, [{"id": entry_id, "message": message}])

    assert _ingest(layout, path) == 1
    out = capsys.readouterr().out
    assert f"  refused: {entry_id}: " in out
    assert "the catalog keeps what it held for each refused entry" in out
    assert "the build's parity check refused it" in out
    assert reason in out
    assert layout.catalog("fr-CA").read_bytes() == before


def test_ingest_run_twice_on_the_same_file_changes_nothing(tmp_path, capsys):
    layout = _real_tree(tmp_path)
    listed = _todo(layout, capsys)
    for entry in listed["entries"]:
        entry["message"] = FRENCH[entry["id"]]
    path = _list_file(tmp_path, listed["entries"])
    assert _ingest(layout, path) == 0
    capsys.readouterr()
    written = layout.catalog("fr-CA")
    before = (written.read_bytes(), written.stat().st_mtime_ns)

    assert _ingest(layout, path) == 0
    assert "0 written, 8 unchanged, 0 refused of 8" in capsys.readouterr().out
    assert (written.read_bytes(), written.stat().st_mtime_ns) == before, (
        "a second run must not even rewrite the file"
    )


def test_ingest_refuses_what_it_cannot_write_by_id_and_writes_the_rest(
    tmp_path, capsys
):
    layout = _real_tree(tmp_path)
    path = _list_file(
        tmp_path,
        [
            {"id": "nav.nowhere", "message": "Nulle part"},
            {"id": "nav.upload"},
            {"id": "nav.queue", "message": "   "},
            {"id": "nav.sure", "message": "Sûr?"},
            {"id": "nav.sure", "message": "Certain?"},
            {"id": "nav.greeting", "english": "Hi {name}", "message": "Salut {name}"},
            {"id": "nav.name", "english": "Name: {name}", "message": "Nom : {name}"},
        ],
    )
    assert _ingest(layout, path) == 1
    out = capsys.readouterr().out
    assert "1 written, 0 unchanged, 5 refused of 6 in todo.json" in out
    assert "nav.nowhere: en.json has no such id" in out
    assert "nav.upload: it has no message" in out
    assert "nav.queue: it has no message" in out
    assert "nav.sure: the file names it more than once" in out
    assert "nav.greeting: its English changed after the list was made" in out
    assert "still to translate: 5 missing, 2 stale" in out

    catalog = _read(layout)
    assert catalog["nav.name"] == _entry(f"Nom{NBSP}: {{name}}", "Name: {name}")
    assert "nav.upload" not in catalog and "nav.sure" not in catalog
    assert catalog["nav.greeting"] == _catalog()["nav.greeting"]
    assert catalog["nav.queue"] == _catalog()["nav.queue"]


def test_a_parity_check_that_cannot_run_refuses_every_entry_and_writes_none(
    tmp_path, capsys, monkeypatch
):
    catalog = _catalog()
    del catalog["nav.gone"]
    layout = _real_tree(tmp_path, catalog)
    before = layout.catalog("fr-CA").read_bytes()

    def broken_worker(layout, args):
        raise REVIEW.ToolError("the coverage worker exited 2: no message")

    monkeypatch.setattr(REVIEW, "call_worker", broken_worker)
    path = _list_file(tmp_path, [{"id": "nav.upload", "message": "Téléverser"}])
    assert _ingest(layout, path) == 1
    assert "nav.upload: the parity check could not run" in capsys.readouterr().out
    assert layout.catalog("fr-CA").read_bytes() == before


def test_ingest_removes_the_dead_entries_even_from_an_empty_list(tmp_path, capsys):
    layout = _real_tree(tmp_path)
    assert _ingest(layout, _list_file(tmp_path, [])) == 0
    assert "dead entries removed: 1" in capsys.readouterr().out
    expected = _catalog()
    del expected["nav.gone"]
    assert _read(layout) == expected


def test_ingest_restamps_a_stale_entry_and_leaves_an_identical_current_one(
    tmp_path, capsys
):
    layout = _real_tree(tmp_path)
    path = _list_file(
        tmp_path,
        [
            {"id": "nav.queue", "message": "Ouvrir la file d’attente de révision"},
            {"id": "nav.greeting", "message": "Salut {name}"},
        ],
    )
    assert _ingest(layout, path) == 0
    assert "1 written, 1 unchanged" in capsys.readouterr().out
    # The same text, now stamped with the hash of the English it was checked against.
    assert _read(layout)["nav.greeting"] == _entry("Salut {name}", "Hello {name}")


@pytest.mark.parametrize(
    ("content", "said"),
    [
        ("{", "could not be read"),
        ("[]", 'an object with "locale" and "entries"'),
        (json.dumps({"locale": "fr-CA"}), 'an object with "locale" and "entries"'),
        (json.dumps({"entries": []}), "is a list for None, not fr-CA"),
        (json.dumps({"locale": "de", "entries": []}), "is a list for 'de', not fr-CA"),
        (
            json.dumps({"locale": "fr-CA", "entries": [{"message": "x"}]}),
            "entry 1 has no id",
        ),
        (json.dumps({"locale": "fr-CA", "entries": ["nav.name"]}), "entry 1 has no id"),
    ],
)
def test_a_file_that_is_no_list_for_the_locale_is_refused_and_nothing_written(
    tmp_path, capsys, content, said
):
    layout = _real_tree(tmp_path)
    before = layout.catalog("fr-CA").read_bytes()
    path = tmp_path / "todo.json"
    path.write_text(content, encoding="utf-8")
    assert _ingest(layout, path) == 2
    assert said in capsys.readouterr().err
    assert layout.catalog("fr-CA").read_bytes() == before


def test_an_absent_list_file_is_a_tool_error(tmp_path, capsys):
    layout = _real_tree(tmp_path)
    assert _ingest(layout, tmp_path / "absent.json") == 2
    assert "❌ ui-i18n-ingest: " in capsys.readouterr().err


# --- drop-stale ---------------------------------------------------------------------


def test_drop_stale_deletes_the_stale_and_dead_entries_and_keeps_the_rest(
    tmp_path, capsys
):
    catalog = _catalog()
    broken = _entry("{mode, select, fast {Rapide}}", EN["nav.mode"]["defaultMessage"])
    catalog["nav.mode"] = broken
    layout = _plain_tree(tmp_path, catalog)

    assert MODULE.main(["drop-stale", "--locale", "fr-CA"], layout=layout) == 0
    out = capsys.readouterr().out
    assert "2 stale and 1 dead entries deleted" in out
    assert "  stale: nav.count\n  stale: nav.greeting\n  dead: nav.gone\n" in out
    # A broken translation of the current English is not stale: the gate's wording says
    # what is wrong with it, and a person decides.
    assert _read(layout) == {"nav.mode": broken, "nav.queue": _catalog()["nav.queue"]}


def test_drop_stale_with_nothing_to_drop_writes_nothing(tmp_path, capsys):
    layout = _plain_tree(tmp_path, {"nav.queue": _catalog()["nav.queue"]})
    written = layout.catalog("fr-CA")
    before = (written.read_bytes(), written.stat().st_mtime_ns)
    assert MODULE.main(["drop-stale", "--locale", "fr-CA"], layout=layout) == 0
    assert "0 stale and 0 dead entries deleted" in capsys.readouterr().out
    assert (written.read_bytes(), written.stat().st_mtime_ns) == before


def test_stale_ids_compare_the_recorded_hash_with_the_englishs():
    english = {**EN, "nav.bad": {"description": "no defaultMessage"}}
    catalog = {
        "nav.queue": _catalog()["nav.queue"],
        "nav.greeting": _catalog()["nav.greeting"],
        "nav.name": {"message": "Nom"},  # no sourceHash at all
        "nav.sure": "Sûr?",  # not an entry
        "nav.bad": {"message": "x", "sourceHash": "0123456789abcdef"},
        "nav.gone": _catalog()["nav.gone"],
    }
    # An entry with no sourceHash, or no entry at all, says nothing of the English it
    # translates, so it is stale too.
    assert MODULE.stale_ids(english, catalog) == [
        "nav.greeting",
        "nav.name",
        "nav.sure",
    ]


# --- what is refused before anything runs ---------------------------------------------


@pytest.mark.parametrize("command", ["todo", "ingest", "drop-stale"])
@pytest.mark.parametrize(
    ("locale", "said"),
    [
        ("en", "en.json is the source catalog"),
        ("../i18n/fr-CA", "there is no"),
        ("de", "there is no"),
    ],
)
def test_the_source_catalog_and_a_name_outside_the_directory_are_refused(
    tmp_path, capsys, monkeypatch, command, locale, said
):
    layout = _plain_tree(tmp_path)
    monkeypatch.setattr(MODULE, "require_worker", lambda layout: None)
    before = {path.name: path.read_bytes() for path in layout.catalog_dir.iterdir()}
    argv = [command, "--locale", locale]
    if command == "ingest":
        argv.append(str(_list_file(tmp_path, [], locale=locale)))
    assert MODULE.main(argv, layout=layout) == 2
    assert f"❌ ui-i18n-{command}: {said}" in capsys.readouterr().err
    assert {
        path.name: path.read_bytes() for path in layout.catalog_dir.iterdir()
    } == before


@pytest.mark.parametrize("command", ["todo", "ingest"])
def test_listing_and_writing_need_the_uis_toolchain(tmp_path, capsys, command):
    layout = _plain_tree(tmp_path)
    argv = [command, "--locale", "fr-CA"]
    if command == "ingest":
        argv.append(str(_list_file(tmp_path, [])))
    assert MODULE.main(argv, layout=layout) == 2
    assert "is missing; run make setup-node first" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [[], ["todo"], ["ingest", "--locale", "fr-CA"], ["translate", "--locale", "fr-CA"]],
)
def test_usage_errors_exit_2(argv):
    with pytest.raises(SystemExit) as exited:
        MODULE.main(argv)
    assert exited.value.code == 2
