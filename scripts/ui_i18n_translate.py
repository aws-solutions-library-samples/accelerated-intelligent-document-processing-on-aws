#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Draft the missing and stale entries of a web UI translation catalog with Amazon Translate.

``make ui-i18n-translate LOCALE=fr-CA`` translates the entries of
``src/ui/src/i18n/<locale>.json`` that the coverage report calls ``missing`` (no
entry) or ``stale`` (the English changed since the entry was translated), and
writes them back with the ``sourceHash`` of the English they were translated from.
Entries that are current are never sent again. Issue #1770.

WHAT THE ENGINE SEES
--------------------
Never ICU. ``src/ui/scripts/i18n/translate-segments.mjs`` (the Node worker, run by
path with ``node``) hoists every plural and select so each branch is a whole
sentence and sends each sentence as HTML, with every argument and ``#`` in a
numbered ``<span translate="no">`` the engine copies untouched and every rich-text
tag as a numbered span whose content it translates. ``TranslateText`` runs from
``en`` to the locale with the formal register where the locale has one
(``FORMALITY``); a result that does not report that register is refused.

THE GLOSSARY
------------
``src/ui/src/i18n/glossary.<locale>.csv`` (``en,<locale>,note``) is imported as the
custom terminology ``idp-ui-<locale>`` and named in every request. Translate reads
every column after the first as a target language, so only the first two are
imported; the note is for the people reviewing the French. The terminology is
re-imported only when the imported text changed (its SHA-256 is kept in the
terminology's description), and that hash is the terminology version the run
records. Translate gives a term the capitals of the English it matched, word by
word, so a Title Case label would get every word of a French term capitalized;
the run puts each multi-word term back in the glossary's own case, keeping only
the first letter's.

WHAT IT WRITES
--------------
The worker rebuilds each answer into an ICU message and judges it with the parity
check the UI build applies (``validate-message.mjs``). Only messages that pass are
written; any other entry is left out, so the screen shows the English, and the run
names it with the reason. Entries the coverage report calls ``invalid`` or ``dead``
are listed and left as they are: the catalog gate already fails on them, and fixing
one is a person's call. The catalog is written sorted by id, two-space JSON.

Every run that calls Translate also writes a provenance file, before the catalog
and never under ``src/`` (by default under the gitignored
``test-reports/ui-i18n-translate/``): the engine, the
Region, the register, the terminology name and hash, the date, and per entry what
was sent, what came back, the terms Translate applied and, with
``--back-translate``, Translate's translation of the answer back into English. The
shipped catalog records none of it. A later review step reads this file.

WHAT IT COSTS
-------------
Translate bills characters sent. Before any call the run counts the characters of
every sentence it will send (with ``--back-translate``, plus an allowance for the
longer answers sent back) and refuses to start when the total is over
``--max-chars`` (by default about five US dollars at the standard rate,
``PRICE_PER_CHARACTER_USD``); the back-translation also stops at the cap.
``--dry-run`` prints the plan and the estimate and calls nothing; ``--limit N``
takes the first N pending entries, for a small trial. It runs with the developer's
own AWS credentials (``--profile``, or the usual AWS environment) in the Region
named by ``--region``; nothing in a deployed stack calls it, and it needs no IAM
change to the solution.

An AWS error stops the run before anything is written, so a run that fails on its
first call (credentials, permissions, Region) costs nothing.

Exit codes:
    0 — every pending entry was written (or ``--dry-run`` printed the plan)
    1 — something needs a person: the engine's answer was refused (the rest were
        written), a message cannot be split for an engine, or the back-translation
        stopped on an AWS error (the catalog and the provenance file are written)
    2 — nothing was written: usage, a missing toolchain, a worker or AWS error during
        the translation, a provenance file that cannot be written, or the cap

USAGE
-----
    python3 scripts/ui_i18n_translate.py --locale fr-CA --dry-run
    python3 scripts/ui_i18n_translate.py --locale fr-CA --region ca-central-1 --profile <name> --limit 20
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

TOOL = "ui-i18n-translate"

#: The register asked of Translate for a locale that has one, and required in its answer.
FORMALITY = {"fr-CA": "FORMAL"}

#: Translate's standard rate for text, in US dollars per character (15 per million).
PRICE_PER_CHARACTER_USD = 15 / 1_000_000

#: The default cap on characters sent in one run: about five US dollars at that rate.
DEFAULT_MAX_CHARS = 333_333

#: How much longer than the English an answer is assumed to be when a back-translation is
#: estimated before the run: French runs 15 to 30 percent longer.
BACK_TRANSLATION_GROWTH = 1.3

#: Indirection for the tests, which replace it rather than running node.
run = subprocess.run


class ToolError(Exception):
    """Nothing can be written: the message says why."""


@dataclass(frozen=True)
class Layout:
    """Where the tool reads and writes, derived from one root so the tests can point it elsewhere."""

    repo_root: Path

    @property
    def ui_root(self) -> Path:
        return self.repo_root / "src" / "ui"

    @property
    def catalog_dir(self) -> Path:
        return self.ui_root / "src" / "i18n"

    @property
    def worker(self) -> Path:
        return self.ui_root / "scripts" / "i18n" / "translate-segments.mjs"

    @property
    def parser_package(self) -> Path:
        """The installed package the worker parses and prints ICU with."""
        return self.ui_root / "node_modules" / "@formatjs" / "icu-messageformat-parser"

    @property
    def provenance_dir(self) -> Path:
        return self.repo_root / "test-reports" / "ui-i18n-translate"

    def catalog(self, locale: str) -> Path:
        return self.catalog_dir / f"{locale}.json"

    def glossary(self, locale: str) -> Path:
        return self.catalog_dir / f"glossary.{locale}.csv"

    def relative(self, path: Path) -> str:
        """``path`` as the messages name it: relative to the repository root."""
        try:
            return str(path.relative_to(self.repo_root))
        except ValueError:
            return str(path)


def call_worker(layout: Layout, args: list[str], request: Any = None) -> Any:
    """Run the Node worker with ``args`` (and ``request`` as JSON on stdin); return its JSON answer."""
    command = ["node", str(layout.worker), *args]
    try:
        result = run(
            command,
            cwd=layout.ui_root,
            input=None if request is None else json.dumps(request),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ToolError(f"node could not be started: {exc}") from None
    if result.returncode != 0:
        raise ToolError(
            f"the worker exited {result.returncode}: {result.stderr.strip() or 'no message'}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError(f"the worker printed no JSON ({exc})") from None


# --- the glossary --------------------------------------------------------------------


@dataclass(frozen=True)
class Terminology:
    """The glossary as Translate imports it: two columns, the note dropped."""

    name: str
    pairs: tuple[tuple[str, str], ...]
    source: Path

    @property
    def data(self) -> bytes:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["en", self.name.removeprefix("idp-ui-")])
        writer.writerows(self.pairs)
        return out.getvalue().encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def read_glossary(path: Path, locale: str) -> Terminology:
    """The glossary's first two columns. Refuses a header other than ``en,<locale>,…``, an
    empty cell, or no terms at all. A byte-order mark (a spreadsheet's export) is ignored."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows or rows[0][:2] != ["en", locale]:
        raise ToolError(f"{path.name}: the header must start with en,{locale}")
    pairs = []
    for number, row in enumerate(rows[1:], start=2):
        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            raise ToolError(
                f"{path.name}:{number}: a term needs its English and its {locale}"
            )
        pairs.append((row[0], row[1]))
    if not pairs:
        raise ToolError(f"{path.name}: the glossary has no terms")
    return Terminology(name=f"idp-ui-{locale}", pairs=tuple(pairs), source=path)


def is_not_found(exc: BaseException) -> bool:
    """Whether an AWS error says the named resource does not exist."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    return (response.get("Error") or {}).get("Code") == "ResourceNotFoundException"


def ensure_terminology(client: Any, terminology: Terminology) -> bool:
    """Import the glossary unless Translate already holds this exact text. True when it imported."""
    description = f"sha256:{terminology.sha256}"
    try:
        held = client.get_terminology(
            Name=terminology.name, TerminologyDataFormat="CSV"
        )
        if (held.get("TerminologyProperties") or {}).get("Description") == description:
            return False
    except Exception as exc:
        if not is_not_found(exc):
            raise
    client.import_terminology(
        Name=terminology.name,
        MergeStrategy="OVERWRITE",
        Description=description,
        TerminologyData={
            "File": terminology.data,
            "Format": "CSV",
            "Directionality": "UNI",
        },
    )
    return True


def _term_key(text: str) -> str:
    """A term as matched: case and apostrophe style do not matter."""
    return text.replace("'", "’").casefold()


def keep_glossary_case(message: str, terminology: Terminology | None) -> str:
    """Undo Translate's word-by-word capitals on a multi-word term: keep the glossary's case,
    and only the first letter's case of what Translate wrote. All-capital text is left alone.

    One pass over the message with every multi-word term, longest first, whole words only,
    so a term inside a longer one is rewritten once, as part of the longer one."""
    if terminology is None:
        return message
    targets = {
        _term_key(target): target for _, target in terminology.pairs if " " in target
    }
    if not targets:
        return message
    alternatives = sorted(targets.values(), key=len, reverse=True)
    pattern = "|".join(
        re.escape(target).replace("’", "['’]") for target in alternatives
    )

    def restore(match: re.Match[str]) -> str:
        found = match.group(0)
        target = targets.get(_term_key(found))
        if target is None or found.isupper():
            return found
        first = target[0].upper() if found[0].isupper() else target[0].lower()
        return first + target[1:]

    return re.sub(rf"(?<!\w)(?:{pattern})(?!\w)", restore, message, flags=re.IGNORECASE)


# --- the plan and the calls ----------------------------------------------------------


def sentences_to_send(plan: dict[str, Any]) -> list[str]:
    """The HTML of every sentence the plan sends, in order (sentences with nothing to translate are kept as they are)."""
    return [
        sentence["html"]
        for entry in plan["pending"]
        for sentence in entry["sentences"]
        if sentence["translate"]
    ]


def describe_plan(plan: dict[str, Any], characters: int) -> list[str]:
    """The plan as the lines the run prints before it calls anything."""
    pending = plan["pending"]
    stale = sum(1 for entry in pending if entry["stale"])
    lines = [
        f"{plan['locale']}: {len(pending)} entr{'y' if len(pending) == 1 else 'ies'} to translate "
        f"({len(pending) - stale} missing, {stale} stale) of {plan['sourceKeys']} in en.json; "
        f"{characters:,} characters, about US${characters * PRICE_PER_CHARACTER_USD:.2f}",
    ]
    if plan["deferred"]:
        lines.append(f"  {plan['deferred']} more left for a later run by --limit")
    for item in plan["unsplittable"]:
        lines.append(f"  needs a hand translation: {item['id']} ({item['reason']})")
    for problem in plan["problems"]:
        lines.append(f"  left as it is, invalid: {problem}")
    for entry_id in plan["dead"]:
        lines.append(f"  left as it is, dead (not in en.json): {entry_id}")
    return lines


def translate(
    client: Any,
    html: str,
    source: str,
    target: str,
    terminology: Terminology | None = None,
) -> dict[str, Any]:
    """One sentence through ``TranslateText``; refuses an answer without the target's register."""
    request: dict[str, Any] = {
        "Text": html,
        "SourceLanguageCode": source,
        "TargetLanguageCode": target,
    }
    formality = FORMALITY.get(target)
    if formality:
        request["Settings"] = {"Formality": formality}
    if terminology is not None:
        request["TerminologyNames"] = [terminology.name]
    response = client.translate_text(**request)
    applied = (response.get("AppliedSettings") or {}).get("Formality")
    if formality and applied != formality:
        raise ToolError(
            f"Translate answered without the {formality} register for {target} "
            f"(AppliedSettings: {applied!r}); nothing was written"
        )
    text = response.get("TranslatedText")
    if not isinstance(text, str):
        raise ToolError(
            "Translate answered without TranslatedText; nothing was written"
        )
    return {
        "text": text,
        "terms": [
            {"source": term.get("SourceText"), "target": term.get("TargetText")}
            for applied_terminology in response.get("AppliedTerminologies") or []
            for term in applied_terminology.get("Terms") or []
        ],
    }


def answer_plan(
    client: Any, plan: dict[str, Any], terminology: Terminology | None
) -> list[dict[str, Any]]:
    """Every pending entry with one answer per sentence, and what was sent and received for it."""
    entries = []
    for entry in plan["pending"]:
        sentences = []
        for sentence in entry["sentences"]:
            if not sentence["translate"]:
                sentences.append({"sent": None, "answer": sentence["html"]})
                continue
            answer = translate(
                client, sentence["html"], "en", plan["locale"], terminology
            )
            sentences.append(
                {
                    "sent": sentence["html"],
                    "answer": answer["text"],
                    "appliedTerms": answer["terms"],
                }
            )
        entries.append(
            {
                "id": entry["id"],
                "english": entry["english"],
                "stale": entry["stale"],
                "sentences": sentences,
            }
        )
    return entries


def back_translate(
    client: Any,
    entries: list[dict[str, Any]],
    locale: str,
    budget: int,
    errors: tuple[type[BaseException], ...],
) -> tuple[int, str | None]:
    """Translate each answer back into English, for the review. Skips an answer that would take
    the run past its budget; stops at the first AWS error, since the forward answers are already
    paid for and must still be written. Returns the characters sent and that error, if any."""
    sent = 0
    error: str | None = None
    for entry in entries:
        for sentence in entry["sentences"]:
            if sentence["sent"] is None:
                continue
            sentence["backTranslation"] = None
            if error is not None or sent + len(sentence["answer"]) > budget:
                continue
            try:
                sentence["backTranslation"] = translate(
                    client, sentence["answer"], locale, "en"
                )["text"]
            except (ToolError, *errors) as exc:
                error = str(exc)
                continue
            sent += len(sentence["answer"])
    return sent, error


def write_catalog(path: Path, catalog: dict[str, Any]) -> None:
    """The catalog as the build reads it: sorted by id, two-space JSON, UTF-8, a final newline.

    Written beside the target and moved over it, so an interrupted write never leaves a
    half-written catalog.
    """
    text = json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True)
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(text + "\n", encoding="utf-8")
    partial.replace(path)


def make_client(region: str, profile: str | None) -> Any:
    """A Translate client for the developer's own credentials. Built only when a run calls AWS."""
    import boto3

    return boto3.Session(profile_name=profile, region_name=region).client("translate")


def aws_errors() -> tuple[type[BaseException], ...]:
    """What a failed AWS call raises; imported late so a dry run needs no AWS library at all."""
    from botocore.exceptions import BotoCoreError, ClientError

    return (BotoCoreError, ClientError)


def now() -> datetime:
    """The run's timestamp; a function so the tests can fix it."""
    return datetime.now(timezone.utc)


def main(argv: list[str] | None = None, *, layout: Layout | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--locale", required=True, help="the catalog to fill, e.g. fr-CA"
    )
    parser.add_argument(
        "--region",
        help="the AWS Region to call Translate in (required unless --dry-run)",
    )
    parser.add_argument("--profile", help="the AWS profile to call it with")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and the estimate; call nothing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="translate only the first N pending entries",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        metavar="N",
        help=f"refuse to start when the run would send more characters (default {DEFAULT_MAX_CHARS:,})",
    )
    parser.add_argument(
        "--back-translate",
        action="store_true",
        help="also translate each answer back into English, into the provenance file",
    )
    parser.add_argument(
        "--provenance",
        type=Path,
        metavar="PATH",
        help="where to write the provenance file (default under test-reports/ui-i18n-translate/)",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit takes a whole number from 1")
    if not args.dry_run and not args.region:
        parser.error(
            "--region is required unless --dry-run (the Region is never guessed)"
        )
    layout = layout or Layout(REPO_ROOT)

    try:
        return translate_catalog(layout, args)
    except ToolError as exc:
        print(f"❌ {TOOL}: {exc}", file=sys.stderr)
        return 2


def provenance_path(
    layout: Layout, args: argparse.Namespace, started: datetime
) -> Path:
    """Where the run's provenance goes: never under ``src/``, which ships."""
    path = args.provenance or layout.provenance_dir / (
        f"{args.locale}-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    if path.resolve().is_relative_to((layout.repo_root / "src").resolve()):
        raise ToolError(
            f"--provenance {layout.relative(path)} is under src/, which ships; "
            f"the provenance stays out of it"
        )
    return path


def write_provenance(path: Path, record: dict[str, Any]) -> None:
    text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ToolError(
            f"the provenance file could not be written, so the catalog was not: {exc}"
        ) from None


def translate_catalog(layout: Layout, args: argparse.Namespace) -> int:
    if not layout.parser_package.is_dir():
        raise ToolError(
            f"{layout.relative(layout.parser_package)} is missing; run make setup-node first"
        )
    started = now()
    provenance = provenance_path(layout, args, started)
    glossary_path = layout.glossary(args.locale)
    terminology = (
        read_glossary(glossary_path, args.locale) if glossary_path.is_file() else None
    )
    if terminology is None:
        print(f"  no {layout.relative(glossary_path)}: running without a terminology")

    plan_args = ["plan", str(layout.catalog_dir), args.locale]
    if args.limit is not None:
        plan_args += ["--limit", str(args.limit)]
    plan = call_worker(layout, plan_args)
    characters = sum(len(html) for html in sentences_to_send(plan))
    estimate = characters + (
        round(characters * BACK_TRANSLATION_GROWTH) if args.back_translate else 0
    )
    for line in describe_plan(plan, estimate):
        print(line)
    if estimate > args.max_chars:
        raise ToolError(
            f"{estimate:,} characters is over --max-chars {args.max_chars:,}; "
            f"take fewer entries with --limit, or raise the cap"
        )
    if args.dry_run:
        return 0
    if not plan["pending"]:
        return 1 if plan["unsplittable"] else 0

    try:
        errors = aws_errors()
    except ImportError as exc:
        raise ToolError(
            f"the AWS SDK is not installed ({exc}); run make setup-venv"
        ) from None
    try:
        client = make_client(args.region, args.profile)
        imported = (
            ensure_terminology(client, terminology)
            if terminology is not None
            else False
        )
        entries = answer_plan(client, plan, terminology)
    except errors as exc:
        raise ToolError(
            f"Translate refused the run, so nothing was written: {exc}"
        ) from None
    if terminology is not None:
        state = "imported" if imported else "unchanged"
        print(
            f"  terminology {terminology.name} {state} (sha256 {terminology.sha256[:12]})"
        )
    back_sent, back_error = (
        back_translate(
            client, entries, args.locale, args.max_chars - characters, errors
        )
        if args.back_translate
        else (0, None)
    )

    request = [
        {
            "id": entry["id"],
            "english": entry["english"],
            "answers": [sentence["answer"] for sentence in entry["sentences"]],
        }
        for entry in entries
    ]
    results = call_worker(layout, ["join", args.locale], {"entries": request})[
        "results"
    ]

    path = layout.catalog(args.locale)
    catalog = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hashes = {entry["id"]: entry["sourceHash"] for entry in plan["pending"]}
    if any(result.get("id") not in hashes for result in results):
        raise ToolError("the worker answered for an entry the plan did not hold")
    refused = [result for result in results if "message" not in result]
    by_id = {entry["id"]: entry for entry in entries}
    for result in results:
        entry = by_id[result["id"]]
        if "message" in result:
            message = keep_glossary_case(result["message"], terminology)
            catalog[result["id"]] = {
                "message": message,
                "sourceHash": hashes[result["id"]],
            }
            entry["message"] = message
        else:
            entry["errors"] = result["errors"]

    write_provenance(
        provenance,
        {
            "engine": "Amazon Translate",
            "operation": "TranslateText",
            "region": args.region,
            "sourceLanguage": "en",
            "targetLanguage": args.locale,
            "formality": FORMALITY.get(args.locale),
            "terminology": None
            if terminology is None
            else {
                "name": terminology.name,
                "sha256": terminology.sha256,
                "glossary": layout.relative(terminology.source),
                "terms": len(terminology.pairs),
            },
            "date": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "characters": {"translated": characters, "backTranslated": back_sent},
            "backTranslationError": back_error,
            "entries": entries,
        },
    )
    write_catalog(path, catalog)

    written = len(results) - len(refused)
    print(
        f"✅ wrote {written} entr{'y' if written == 1 else 'ies'} to {layout.relative(path)}; "
        f"provenance in {layout.relative(provenance)}"
    )
    for result in refused:
        for error in result["errors"]:
            print(f"  left out (English shown): {error}")
    if back_error:
        print(
            f"  the back-translation stopped on an AWS error, so the provenance lacks "
            f"some of it: {back_error}"
        )
    return 1 if refused or plan["unsplittable"] or back_error else 0


if __name__ == "__main__":
    sys.exit(main())
