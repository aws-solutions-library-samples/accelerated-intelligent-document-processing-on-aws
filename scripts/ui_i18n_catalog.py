#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""List, write and drop the translations of a web UI catalog, in the change that edits the English.

The translations in ``src/ui/src/i18n/<locale>.json`` are written in the same change as
the English they translate: the coding agent, which has just edited the screen, writes
them, and these three commands do the mechanics. They call no AWS service and no model.
Issue #2080.

    todo        print, as JSON, every entry of the catalog to translate
    ingest      write the translations of such a list, given a ``message`` per entry
    drop-stale  delete the stale entries, so the English shows until they are translated

WHAT IS LISTED
--------------
``todo`` lists what ``make ui-i18n-revise`` sends with ``--refresh`` (``plan_revision``
in ``ui_i18n_revise.py``): every id ``en.json`` has and the catalog lacks, and every
entry the coverage worker calls stale. It adds the stale entries the revise tool leaves
out because the old translation no longer compiles: a reworded message whose argument,
plural or tag changed is in that state in the change that rewords it, and the coverage
worker lists it as invalid only. So a stale entry here is one whose recorded
``sourceHash`` is not the hash of its English as it is now (``stale_ids``), whether or
not it compiles; an entry with no ``sourceHash`` at all is stale too, since nothing says
which English it translates. The catalog gate, ``scripts/check_ui_i18n_catalog.py``,
fails on the same entries.

The list is one JSON object on stdout and nothing else is printed there:
``{"locale": "<locale>", "entries": [...]}``, each entry in id order with ``id``,
``english``, ``description``, ``glossaryTerms`` (the rows of ``glossary.<locale>.csv``
whose English term the message uses) and, for a stale one, ``"stale": true`` and its
``translation``, the text it holds for the older English.

WHAT IS WRITTEN
---------------
``ingest FILE`` reads that object with a ``message`` added to each entry; an entry needs
only its ``id`` and its ``message``. An entry is refused, with the reason, when en.json
has no such id, the file names it twice, it has no message, or the ``english`` the file
gives is no longer the English. Every other message gets the locale's spacing before
punctuation (``as_written`` in the revise tool) and the UI build's parity check, run by
the coverage worker over a scratch catalog (``check_answers`` in the revise tool). One
that fails is refused with the build's wording, and its entry keeps its text; one that
passes is written with the ``sourceHash`` of its English. Dead entries (ids en.json no
longer has, which the build rejects) are removed. The translation tool's writer
(``write_catalog``) writes the catalog once, and only when it changed, so a second run
on the same file changes nothing.

``drop-stale`` deletes every stale entry and every dead one through the same writer, so
the screen shows the English until a later change translates it again.

Exit codes:
    0 — the list was printed, every entry of the file was written or already held, or
        the stale and dead entries were deleted
    1 — some entries of the file were refused, each named with the reason; the rest
        were written
    2 — nothing was done: usage, a missing toolchain or catalog, or a file that is not
        a list for this locale

USAGE
-----
    python3 scripts/ui_i18n_catalog.py todo --locale fr-CA > todo.json
    python3 scripts/ui_i18n_catalog.py ingest --locale fr-CA todo.json
    python3 scripts/ui_i18n_catalog.py drop-stale --locale fr-CA
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# The revise tool's selection, entries and parity check, the review's glossary and
# layout, and the translation tool's catalog writer, used as they are.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ui_i18n_review as review  # noqa: E402
import ui_i18n_revise as revise  # noqa: E402
from ui_i18n_translate import write_catalog  # noqa: E402

ToolError = review.ToolError


def english_of(english: Mapping[str, Any], entry_id: str) -> str | None:
    """The English ``defaultMessage`` of an id, or None when en.json has none for it."""
    source = english.get(entry_id)
    if isinstance(source, dict) and isinstance(source.get("defaultMessage"), str):
        return source["defaultMessage"]
    return None


def records_other_english(message: str, entry: Any) -> bool:
    """Whether a catalog entry records the ``sourceHash`` of an English other than ``message``."""
    recorded = entry.get("sourceHash") if isinstance(entry, dict) else None
    return recorded != revise.source_hash(message)


def stale_ids(english: Mapping[str, Any], catalog: Mapping[str, Any]) -> list[str]:
    """The catalog's stale entries, sorted: those whose recorded ``sourceHash`` is not the
    hash of their English as it is now, whether or not they still compile."""
    return sorted(
        entry_id
        for entry_id, entry in catalog.items()
        if (message := english_of(english, entry_id)) is not None
        and records_other_english(message, entry)
    )


def dead_ids(english: Mapping[str, Any], catalog: Mapping[str, Any]) -> list[str]:
    """The catalog's entries whose id en.json no longer has, sorted."""
    return sorted(set(catalog) - set(english))


def target(
    entry_id: str,
    english: Mapping[str, Any],
    catalog: Mapping[str, Any],
    glossary: review.Glossary | None,
) -> revise.Target:
    """One entry as the revise tool holds it; stale when the catalog holds it for another English."""
    message = english[entry_id]["defaultMessage"]
    return revise.Target(
        id=entry_id,
        english=message,
        description=str(english[entry_id].get("description") or ""),
        before=revise.current_text(catalog, entry_id),
        terms=glossary.terms_in(message) if glossary else [],
        stale=entry_id in catalog and records_other_english(message, catalog[entry_id]),
    )


def entries(count: int) -> str:
    return f"{count} entr{'y' if count == 1 else 'ies'}"


# --- the three commands ----------------------------------------------------------------


def catalog_path(layout: review.Layout, locale: str) -> Path:
    """The locale's catalog. en.json is the source and is never written here, and a name
    that is not a file in the catalog directory is refused."""
    if locale == "en":
        raise ToolError("en.json is the source catalog; make ui-i18n-extract writes it")
    path = layout.catalog(locale)
    if path.parent != layout.catalog_dir or not path.is_file():
        raise ToolError(f"there is no {layout.relative(path)} to work on")
    return path


def require_worker(layout: review.Layout) -> None:
    """The coverage worker needs the UI's ICU parser."""
    if not layout.parser_package.is_dir():
        raise ToolError(
            f"{layout.relative(layout.parser_package)} is missing; run make setup-node first"
        )


def todo(layout: review.Layout, locale: str) -> int:
    require_worker(layout)
    path = catalog_path(layout, locale)
    glossary_path = layout.glossary(locale)
    glossary = (
        review.read_glossary(glossary_path, locale) if glossary_path.is_file() else None
    )
    plan = revise.plan_revision(layout, locale, glossary, [], {}, None, refresh=True)
    english = review.read_json(layout.catalog("en"))
    catalog = review.read_json(path)
    stale = set(stale_ids(english, catalog))
    broken = [
        target(entry_id, english, catalog, glossary)
        for entry_id in plan.invalid
        if entry_id in stale
    ]

    listed = []
    for item in sorted(plan.targets + broken, key=lambda item: item.id):
        terms = set(item.terms)
        entry: dict[str, Any] = {
            "id": item.id,
            "english": item.english,
            "description": item.description,
            "glossaryTerms": [
                {"en": term, locale: translation, "note": note}
                for term, translation, note in (glossary.rows if glossary else ())
                if term in terms
            ],
        }
        if item.stale:
            entry["stale"] = True
            if item.before is not None:
                entry["translation"] = item.before
        listed.append(entry)
    sys.stdout.write(
        json.dumps({"locale": locale, "entries": listed}, ensure_ascii=False, indent=2)
        + "\n"
    )
    # What to do next goes to stderr, so stdout stays the JSON.
    stale_count = sum(1 for entry in listed if entry.get("stale"))
    hint = f"nothing to translate in {layout.relative(path)}"
    if listed:
        hint = (
            f"{entries(len(listed))} to translate in {layout.relative(path)}, "
            f'{stale_count} of them stale: give each a "message" and run '
            f"make ui-i18n-ingest LOCALE={locale} FILE=<file>"
        )
    if stale_count:
        hint += (
            f", or delete the stale ones with make ui-i18n-drop-stale LOCALE={locale}"
        )
    dead = len(dead_ids(english, catalog))
    if dead:
        hint += (
            f"; make ui-i18n-drop-stale LOCALE={locale} deletes the {dead} dead "
            f"entr{'y' if dead == 1 else 'ies'}, which en.json no longer has"
        )
    print(hint, file=sys.stderr)
    return 0


def read_items(source: Path, locale: str) -> list[dict[str, Any]]:
    """The entries of a list for ``ingest``: the object ``todo`` prints, for this locale.
    Refuses another shape, another locale, or an entry without an id."""
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError(f"{source} could not be read: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ToolError(
            f"{source.name} is not a list as make ui-i18n-todo prints it: an object "
            f'with "locale" and "entries"'
        )
    if data.get("locale") != locale:
        raise ToolError(
            f"{source.name} is a list for {data.get('locale')!r}, not {locale}: it "
            f'needs "locale": "{locale}", as make ui-i18n-todo writes it'
        )
    items = data["entries"]
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ToolError(f"{source.name}: entry {number} has no id")
    return items


def ingest(layout: review.Layout, locale: str, source: Path) -> int:
    require_worker(layout)
    path = catalog_path(layout, locale)
    items = read_items(source, locale)
    english = review.read_json(layout.catalog("en"))
    catalog = review.read_json(path)
    held = dict(catalog)

    named = Counter(item["id"] for item in items)
    refused: dict[str, str] = {}
    answered: list[revise.Target] = []
    for item in items:
        entry_id = item["id"]
        if english_of(english, entry_id) is None:
            refused[entry_id] = "en.json has no such id"
        elif named[entry_id] > 1:
            refused[entry_id] = "the file names it more than once"
        elif error := revise.check_revision(item):
            refused[entry_id] = error
        elif "english" in item and item["english"] != english_of(english, entry_id):
            refused[entry_id] = (
                "its English changed after the list was made: translate the English "
                "make ui-i18n-todo lists now"
            )
        else:
            answer = target(entry_id, english, catalog, None)
            answer.after = revise.as_written(answer.english, item["message"], locale)
            answered.append(answer)
    revise.check_answers(layout, locale, answered)

    for answer in answered:
        if answer.outcome == "revised":
            catalog[answer.id] = {
                "message": answer.after,
                "sourceHash": revise.source_hash(answer.english),
            }
        elif answer.outcome == "refused":
            refused[answer.id] = answer.reason
    dead = dead_ids(english, catalog)
    for entry_id in dead:
        del catalog[entry_id]
    if catalog != held:
        write_catalog(path, catalog)

    written = sum(1 for answer in answered if answer.outcome == "revised")
    unchanged = sum(1 for answer in answered if answer.outcome == "unchanged")
    print(
        f"{'⚠️ ' if refused else '✅'} {layout.relative(path)}: {written} written, "
        f"{unchanged} unchanged, {len(refused)} refused of {len(named)} in {source.name}; "
        f"dead entries removed: {len(dead)}"
    )
    for entry_id, reason in refused.items():
        print(f"  refused: {entry_id}: {reason}")
    if refused:
        print(
            "  the catalog keeps what it held for each refused entry; correct the "
            "list and run the same command again"
        )
    missing = len(set(english) - set(catalog))
    stale = len(stale_ids(english, catalog))
    if missing or stale:
        gate = " (the catalog gate fails on the stale ones)" if stale else ""
        print(
            f"  still to translate: {missing} missing, {stale} stale{gate}; "
            f"make ui-i18n-todo LOCALE={locale} lists them"
        )
    return 1 if refused else 0


def drop_stale(layout: review.Layout, locale: str) -> int:
    path = catalog_path(layout, locale)
    english = review.read_json(layout.catalog("en"))
    catalog = review.read_json(path)
    stale = stale_ids(english, catalog)
    dead = dead_ids(english, catalog)
    for entry_id in stale + dead:
        del catalog[entry_id]
    if stale or dead:
        write_catalog(path, catalog)
    print(
        f"✅ {layout.relative(path)}: {len(stale)} stale and {len(dead)} dead entries "
        "deleted"
        + ("; the English shows for them until they are translated" if stale else "")
    )
    for entry_id in stale:
        print(f"  stale: {entry_id}")
    for entry_id in dead:
        print(f"  dead: {entry_id}")
    return 0


def main(argv: list[str] | None = None, *, layout: review.Layout | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser(
        "todo", help="print the missing and stale entries as JSON"
    )
    writing = commands.add_parser(
        "ingest",
        help="write the translations of a todo list, given a message per entry",
    )
    writing.add_argument(
        "file", type=Path, help="the todo list, with a message per entry"
    )
    dropping = commands.add_parser(
        "drop-stale", help="delete the stale and the dead entries"
    )
    for command in (listing, writing, dropping):
        command.add_argument(
            "--locale", required=True, help="the catalog to work on, e.g. fr-CA"
        )
    args = parser.parse_args(argv)
    layout = layout or review.Layout(review.REPO_ROOT)

    try:
        if args.command == "todo":
            return todo(layout, args.locale)
        if args.command == "ingest":
            return ingest(layout, args.locale, args.file)
        return drop_stale(layout, args.locale)
    except ToolError as exc:
        print(f"❌ ui-i18n-{args.command}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
