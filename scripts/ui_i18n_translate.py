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

WHAT IT WRITES
--------------
The worker rebuilds each answer into an ICU message and judges it with the parity
check the UI build applies (``validate-message.mjs``). Only messages that pass are
written; any other entry is left out, so the screen shows the English, and the run
names it with the reason. Entries the coverage report calls ``invalid`` or ``dead``
are listed and left as they are: the catalog gate already fails on them, and fixing
one is a person's call. The catalog is written sorted by id, two-space JSON.

WHAT IT COSTS
-------------
Translate bills characters sent. Before any call the run counts the characters of
every sentence it will send and refuses to start when the total is over
``--max-chars`` (by default about five US dollars at the standard rate,
``PRICE_PER_CHARACTER_USD``). ``--dry-run`` prints the plan and the estimate and
calls nothing; ``--limit N`` takes the first N pending entries, for a small trial.
It runs with the developer's own AWS credentials (``--profile``, or the usual AWS
environment) in the Region named by ``--region``; nothing in a deployed stack calls
it, and it needs no IAM change to the solution.

An AWS error stops the run before anything is written, so a run that fails on its
first call (credentials, permissions, Region) costs nothing.

Exit codes:
    0 — every pending entry was written (or ``--dry-run`` printed the plan)
    1 — some entries need a person: the engine's answer was refused (the rest were
        written), or a message cannot be split for an engine (listed either way)
    2 — nothing was written: usage, a missing toolchain, a worker or AWS error, or the cap

USAGE
-----
    python3 scripts/ui_i18n_translate.py --locale fr-CA --dry-run
    python3 scripts/ui_i18n_translate.py --locale fr-CA --region ca-central-1 --profile <name> --limit 20
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
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

    def catalog(self, locale: str) -> Path:
        return self.catalog_dir / f"{locale}.json"

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


def translate(client: Any, html: str, locale: str) -> str:
    """One sentence through ``TranslateText``; refuses an answer without the locale's register."""
    request: dict[str, Any] = {
        "Text": html,
        "SourceLanguageCode": "en",
        "TargetLanguageCode": locale,
    }
    formality = FORMALITY.get(locale)
    if formality:
        request["Settings"] = {"Formality": formality}
    response = client.translate_text(**request)
    applied = (response.get("AppliedSettings") or {}).get("Formality")
    if formality and applied != formality:
        raise ToolError(
            f"Translate answered without the {formality} register for {locale} "
            f"(AppliedSettings: {applied!r}); nothing was written"
        )
    text = response.get("TranslatedText")
    if not isinstance(text, str):
        raise ToolError(
            "Translate answered without TranslatedText; nothing was written"
        )
    return text


def answer_plan(client: Any, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The join request: every pending entry with one answer per sentence."""
    entries = []
    for entry in plan["pending"]:
        answers = [
            translate(client, sentence["html"], plan["locale"])
            if sentence["translate"]
            else sentence["html"]
            for sentence in entry["sentences"]
        ]
        entries.append(
            {"id": entry["id"], "english": entry["english"], "answers": answers}
        )
    return entries


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


def translate_catalog(layout: Layout, args: argparse.Namespace) -> int:
    if not layout.parser_package.is_dir():
        raise ToolError(
            f"{layout.relative(layout.parser_package)} is missing; run make setup-node first"
        )
    plan_args = ["plan", str(layout.catalog_dir), args.locale]
    if args.limit is not None:
        plan_args += ["--limit", str(args.limit)]
    plan = call_worker(layout, plan_args)
    characters = sum(len(html) for html in sentences_to_send(plan))
    for line in describe_plan(plan, characters):
        print(line)
    if characters > args.max_chars:
        raise ToolError(
            f"{characters:,} characters is over --max-chars {args.max_chars:,}; "
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
        entries = answer_plan(client, plan)
    except errors as exc:
        raise ToolError(
            f"Translate refused the run, so nothing was written: {exc}"
        ) from None
    results = call_worker(layout, ["join", args.locale], {"entries": entries})[
        "results"
    ]

    path = layout.catalog(args.locale)
    catalog = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hashes = {entry["id"]: entry["sourceHash"] for entry in plan["pending"]}
    if any(result.get("id") not in hashes for result in results):
        raise ToolError("the worker answered for an entry the plan did not hold")
    refused = [result for result in results if "message" not in result]
    for result in results:
        if "message" in result:
            catalog[result["id"]] = {
                "message": result["message"],
                "sourceHash": hashes[result["id"]],
            }
    write_catalog(path, catalog)
    written = len(results) - len(refused)
    print(
        f"✅ wrote {written} entr{'y' if written == 1 else 'ies'} to {layout.relative(path)}"
    )
    for result in refused:
        for error in result["errors"]:
            print(f"  left out (English shown): {error}")
    return 1 if refused or plan["unsplittable"] else 0


if __name__ == "__main__":
    sys.exit(main())
