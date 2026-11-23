#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Revise the entries of a web UI translation catalog that a review flagged, with a Bedrock model.

``make ui-i18n-revise LOCALE=fr-CA`` has the model of ``make ui-i18n-review`` write again
the entries a review flagged, and writes each answer that passes the UI build's parity
check into ``src/ui/src/i18n/<locale>.json``. A person reviews the result before it is
committed. Issue #1770.

WHAT IS REVISED
---------------
``--review PATH`` (repeatable) takes the ``report.json`` of a review run: its
disagreements, each with the review's problems, reason and suggested translation, and
the entries it could not review. An entry whose translation changed after that review
is left out, so the run never writes over a later edit. ``--notes PATH`` takes a JSON
object of message id to a note: what a person found where the message shows. A note
adds its entry to the run, and may name an entry the catalog lacks, which is then
drafted from the English. Entries the coverage worker calls invalid or stale are listed
and never sent, as in the review.

WHAT THE MODEL IS ASKED
-----------------------
The task, the review's rules for the locale (``STYLE_RULES`` in ``ui_i18n_review.py``),
the locale's rules for a revision (``REVISE_RULES``: for ``fr-CA``, Canadian French
wherever it fits) and the whole glossary open every request and end in a cache point.
Per entry: the English, the developer's description of where it appears, the current
translation, the glossary terms its English uses, and the review's findings and the
note when there are any. The model answers through one tool (``TOOL_NAME``) with the
message only, per entry.

WHAT IS WRITTEN
---------------
The coverage worker (``src/ui/scripts/i18n/coverage.mjs``) checks every answer over a
scratch catalog with the parity check the UI build applies: ICU that parses, and the
English's arguments, their kinds and its rich-text tags. An answer that fails, or that
is not well formed, is refused with the reason, and its entry keeps the text it had.
An answer that passes and differs from that text is written, with the ``sourceHash`` of
its English; one that passes unchanged is counted as unchanged.

Every run that calls Bedrock writes a provenance file, before the catalog and never under
``src/`` (by default under the gitignored ``test-reports/ui-i18n-revise/``, or where
``--provenance PATH`` says): the model, the Region, the SHA-256
of the prompt and of the glossary, the reviews and notes it read, the cost, and per
entry the text before and after and whether it was revised, unchanged or refused, and
why. An entry it wrote carries ``message``, as in the translation tool's provenance, so
``make ui-i18n-review`` with ``--provenance <file>`` reviews exactly those entries.

WHAT IT COSTS
-------------
As the review: tokens are priced from ``config_library/pricing.yaml``; before any call
the run estimates its cost and refuses to start above ``--max-usd`` (US$20 by default);
during the run it stops before any batch that could take it past the cap, counting that
batch at its largest possible size, every retry included. ``--dry-run`` prints the plan
and the estimate and calls nothing; ``--limit N`` takes the first N entries. It runs with
the developer's own AWS credentials in the Region named by ``--region``.

Exit codes:
    0 — every entry sent was answered and checked, and none was refused, or a dry run
        whose estimate is within the cap
    1 — something needs a person: an answer was refused, or the cap or an AWS error
        stopped the run (what passed is written, and the rest is listed)
    2 — nothing was written: usage, a missing toolchain or catalog, a model with no
        price, an estimate over the cap, an AWS error before any answer, or a
        provenance file that cannot be written

USAGE
-----
    python3 scripts/ui_i18n_revise.py --locale fr-CA --dry-run \\
        --review test-reports/ui-i18n-review/fr-CA-<stamp>/report.json
    python3 scripts/ui_i18n_revise.py --locale fr-CA --region ca-central-1 --profile <name> \\
        --review test-reports/ui-i18n-review/fr-CA-<stamp>/report.json --notes notes.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# The review's pieces (the coverage worker, the glossary, the price, the Bedrock
# client) and the translation tool's catalog writer, used as they are.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ui_i18n_review as review  # noqa: E402
from ui_i18n_translate import write_catalog  # noqa: E402

ToolError = review.ToolError

TOOL = "ui-i18n-revise"

#: The review outcomes that make an entry a target.
FLAGGED = ("disagreement", "unreviewed")

#: The one tool the model answers through.
TOOL_NAME = "record_revision"

#: Each locale's rules for a revision, after the review's ``STYLE_RULES``. A locale
#: with none cannot be revised.
REVISE_RULES = {
    "fr-CA": (
        "- Canadian French wherever it fits: Quebec usage and vocabulary, for example "
        "courriel, clavardage, téléverser and bogue, and never France usage where a "
        "Quebec usage exists.\n"
        "- Canadian typography: a no-break space (U+00A0) before a colon that ends a "
        "phrase, no space before ; ? and !, and a no-break space inside « and »."
    ),
}

#: What every outcome is called, in the order the report counts them.
OUTCOMES = ("revised", "unchanged", "refused", "skipped")


@dataclass
class Target:
    """One entry to revise, and what became of it."""

    id: str
    english: str
    description: str
    #: The current translation, or None when the catalog has none.
    before: str | None
    terms: list[str]
    #: The review's problems, reason and suggestion, for a disagreement.
    flagged: dict[str, Any] | None = None
    note: str = ""
    after: str | None = None
    outcome: str = "skipped"
    reason: str = ""

    def request(self) -> dict[str, Any]:
        """The entry as the model receives it."""
        item: dict[str, Any] = {
            "id": self.id,
            "english": self.english,
            "description": self.description,
        }
        if self.before is not None:
            item["translation"] = self.before
        if self.terms:
            item["glossaryTerms"] = self.terms
        if self.flagged:
            item["review"] = self.flagged
        if self.note:
            item["note"] = self.note
        return item

    @property
    def size(self) -> int:
        return len(json.dumps(self.request(), ensure_ascii=False))


@dataclass
class Plan:
    locale: str
    targets: list[Target]
    deferred: int
    invalid: list[str]
    stale: list[str]
    #: Flagged by a review, but edited after it: left out.
    changed: list[str]


# --- what to revise ----------------------------------------------------------------------


def read_review(path: Path, locale: str) -> list[dict[str, Any]]:
    """The flagged entries of one review report. Refuses a file that is not the
    ``report.json`` of a review of ``locale``."""
    data = review.read_json(path)
    if data.get("tool") != review.TOOL or not isinstance(data.get("entries"), list):
        raise ToolError(f"{path} is not the report.json of a make ui-i18n-review run")
    if data.get("locale") != locale:
        raise ToolError(f"{path} reviews {data.get('locale')!r}, not {locale}")
    flagged = []
    for entry in data["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ToolError(f"{path} holds an entry without an id")
        if entry.get("outcome") in FLAGGED:
            flagged.append(entry)
    return flagged


def read_notes(path: Path) -> dict[str, str]:
    """A JSON object of message id to a note; an empty note is allowed."""
    notes = {}
    for entry_id, note in review.read_json(path).items():
        if not isinstance(note, str):
            raise ToolError(f"{path.name}: the note for {entry_id} is not text")
        notes[entry_id] = note.strip()
    return notes


def findings(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """What a review found in one flagged entry; an unreviewed entry has no findings."""
    if entry.get("outcome") != "disagreement":
        return None
    return {
        "problems": [str(problem) for problem in entry.get("problems") or []],
        "reason": str(entry.get("reason") or ""),
        "suggestion": str(entry.get("rendering") or ""),
    }


def current_text(catalog: Mapping[str, Any], entry_id: str) -> str | None:
    value = catalog.get(entry_id)
    if isinstance(value, dict) and isinstance(value.get("message"), str):
        return value["message"]
    return None


def plan_revision(
    layout: review.Layout,
    locale: str,
    glossary: review.Glossary | None,
    reports: list[list[dict[str, Any]]],
    notes: Mapping[str, str],
    limit: int | None,
) -> Plan:
    """The entries to send, in id order: every entry a review flagged and every entry a
    note names, less the ones the build rejects, the stale ones and the ones edited
    after their review."""
    path = layout.catalog(locale)
    if not path.is_file():
        raise ToolError(
            f"there is no {layout.relative(path)} to revise; draft one with make ui-i18n-translate"
        )
    english = review.read_json(layout.catalog("en"))
    catalog = review.read_json(path)
    unknown = sorted(set(notes) - set(english))
    if unknown:
        raise ToolError(
            f"the notes name ids en.json does not have: {', '.join(unknown)}"
        )
    report = review.call_worker(
        layout, [str(layout.catalog_dir), "--json", "--locale", locale]
    )
    coverage = (report.get("locales") or {}).get(locale)
    if not isinstance(coverage, dict):
        raise ToolError(f"the coverage worker reported nothing for {locale}")
    invalid = sorted(coverage.get("invalid") or [])
    stale = sorted(coverage.get("stale") or [])

    flagged: dict[str, dict[str, Any] | None] = {}
    changed: set[str] = set()
    for entries in reports:
        for entry in entries:
            if current_text(catalog, entry["id"]) != entry.get("translation"):
                changed.add(entry["id"])
            else:
                flagged[entry["id"]] = findings(entry)
    left_out = set(invalid) | set(stale)
    ids = sorted(
        entry_id
        for entry_id in set(flagged) | set(notes)
        if entry_id in english and entry_id not in left_out
    )
    targets = []
    for entry_id in ids:
        source = english[entry_id]
        message = source["defaultMessage"]
        targets.append(
            Target(
                id=entry_id,
                english=message,
                description=str(source.get("description") or ""),
                before=current_text(catalog, entry_id),
                terms=glossary.terms_in(message) if glossary else [],
                flagged=flagged.get(entry_id),
                note=notes.get(entry_id, ""),
            )
        )
    kept = targets if limit is None else targets[:limit]
    return Plan(
        locale=locale,
        targets=kept,
        deferred=len(targets) - len(kept),
        invalid=invalid,
        stale=stale,
        changed=sorted(changed - set(notes)),
    )


def make_batches(targets: list[Target]) -> list[list[Target]]:
    """As the review batches: at most ``BATCH_ENTRIES`` entries and ``BATCH_CHARS``
    characters per batch, in order; an entry larger than the limit goes alone."""
    batches: list[list[Target]] = []
    current: list[Target] = []
    chars = 0
    for target in targets:
        if current and (
            len(current) == review.BATCH_ENTRIES
            or chars + target.size > review.BATCH_CHARS
        ):
            batches.append(current)
            current, chars = [], 0
        current.append(target)
        chars += target.size
    if current:
        batches.append(current)
    return batches


# --- the request -----------------------------------------------------------------------

#: The tool's input schema: what an answer must look like.
TOOL_SPEC = {
    "name": TOOL_NAME,
    "description": "Record the revised translation of every message you were given, once each.",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "revisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "message": {"type": "string"},
                        },
                        "required": ["id", "message"],
                    },
                }
            },
            "required": ["revisions"],
        }
    },
}


def instructions(locale: str, glossary: review.Glossary | None) -> str:
    """The cached prefix of every request: the task, the rules and the glossary."""
    # shortcut: the spacing before punctuation is asked of the model, not applied by
    # code as the translation tool applies it; apply it by code if revised entries miss it.
    text = (
        f"You revise {locale} translations of the user interface of a web application, "
        "the GenAI Intelligent Document Processing accelerator: people upload documents, "
        "check the data extracted from them in review queues, test and evaluate "
        "configurations, and chat with agents.\n\n"
        "Each message you are given has an id, the English source in ICU MessageFormat, "
        "a description by the developer of where it appears, the current translation "
        "(absent when the message has none yet) and the glossary terms its English uses. "
        "Some also carry review: the problems a review found in the current translation, "
        "its reason and its suggested translation. Some carry note: what a person found "
        "where the message shows. Both are evidence: judge the message yourself.\n\n"
        "For each message, write its best translation under the rules below: keep what "
        "the current translation gets right, and correct what it gets wrong.\n\n"
        "Keep the ICU MessageFormat of the English exactly: every argument in braces "
        "({name}, {count, number}), every plural and select with its branch keys, # inside "
        "a plural, and every tag such as <b>...</b>. Translate only the text around and "
        "inside them. An ASCII apostrophe that quotes ICU syntax in the English, as in "
        "'<No content>', stays around the same syntax; every other apostrophe is the "
        "typographic ’. The answer must parse as ICU MessageFormat.\n\n"
        f"Rules for {locale}:\n{review.STYLE_RULES[locale]}\n{REVISE_RULES[locale]}\n\n"
        f"Answer by calling the {TOOL_NAME} tool once, with one revision per message id "
        "you were given, holding the id and the translated message only, and write "
        "nothing else."
    )
    if glossary is not None:
        text += (
            f"\n\nGlossary (English term, {locale} term, note for reviewers):\n"
            + glossary.as_csv(locale)
        )
    return text


def prompt_sha256(prefix: str) -> str:
    """What the provenance records of the prompt: the cached prefix and the tool."""
    text = prefix + "\n" + json.dumps(TOOL_SPEC, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def batch_text(batch: list[Target]) -> str:
    items = [target.request() for target in batch]
    return f"Revise these {len(batch)} messages:\n" + json.dumps(
        items, ensure_ascii=False
    )


def converse_request(model: str, prefix: str, batch: list[Target]) -> dict[str, Any]:
    """Shaped as the review's request, for the same model."""
    return {
        "modelId": model,
        "system": [{"text": prefix}, {"cachePoint": {"type": "default"}}],
        "messages": [{"role": "user", "content": [{"text": batch_text(batch)}]}],
        "toolConfig": {"tools": [{"toolSpec": TOOL_SPEC}], "toolChoice": {"auto": {}}},
        "inferenceConfig": {"maxTokens": review.MAX_OUTPUT_TOKENS},
        "additionalModelRequestFields": {"output_config": {"effort": review.EFFORT}},
    }


def prefix_chars(prefix: str) -> int:
    """The characters every request opens with: the instructions and the tool's schema."""
    return len(prefix) + len(json.dumps(TOOL_SPEC))


def estimate_usd(
    rates: review.Rates, prefix: str, batches: list[list[Target]]
) -> float:
    """As the review estimates: the prefix written to the cache once and read after, the
    entries at the input rate, and ``OUTPUT_TOKENS_PER_ENTRY`` per entry."""
    total = 0.0
    shared = prefix_chars(prefix) / review.CHARS_PER_TOKEN
    for index, batch in enumerate(batches):
        total += shared * (rates.cache_write if index == 0 else rates.cache_read)
        total += len(batch_text(batch)) / review.CHARS_PER_TOKEN * rates.input
        total += len(batch) * review.OUTPUT_TOKENS_PER_ENTRY * rates.output
    return total


def ceiling_usd(rates: review.Rates, prefix: str, batch: list[Target]) -> float:
    """The most one batch can cost: every input token at the dearest input rate, the
    output at its limit, every attempt botocore may make."""
    tokens = (
        prefix_chars(prefix) + len(batch_text(batch))
    ) / review.FEWEST_CHARS_PER_TOKEN
    one = tokens * max(rates.input, rates.cache_write)
    return review.ATTEMPTS * (one + review.MAX_OUTPUT_TOKENS * rates.output)


# --- the answer ------------------------------------------------------------------------


def check_revision(item: Mapping[str, Any]) -> str | None:
    """Why one revision cannot be used, or None."""
    message = item.get("message")
    if not isinstance(message, str) or not message.strip():
        return "it has no message"
    if len(message) > review.MAX_RENDERING_CHARS:
        return f"its message is longer than {review.MAX_RENDERING_CHARS} characters"
    return None


def read_answer(
    response: Mapping[str, Any], batch: list[Target]
) -> tuple[dict[str, str], dict[str, str]]:
    """The usable messages by id, and why each other id of the batch has none."""
    ids = {target.id for target in batch}
    stop = response.get("stopReason")
    if stop == "max_tokens":
        return {}, {
            entry_id: "the answer was cut off at the token limit" for entry_id in ids
        }
    content = ((response.get("output") or {}).get("message") or {}).get("content") or []
    calls = [
        block["toolUse"]
        for block in content
        if isinstance(block, dict)
        and isinstance(block.get("toolUse"), dict)
        and block["toolUse"].get("name") == TOOL_NAME
    ]
    if not calls:
        reason = (
            f"the model answered without the {TOOL_NAME} tool (stop reason {stop!r})"
        )
        return {}, {entry_id: reason for entry_id in ids}
    messages: dict[str, str] = {}
    unusable: dict[str, str] = {}
    seen: set[str] = set()
    for call in calls:
        arguments = call.get("input")
        items = arguments.get("revisions") if isinstance(arguments, dict) else None
        for item in items if isinstance(items, list) else []:
            entry_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(entry_id, str) or entry_id not in ids:
                continue  # an answer for no entry of this batch counts for nothing
            if entry_id in seen:
                messages.pop(entry_id, None)
                unusable[entry_id] = "the model answered for it twice"
                continue
            seen.add(entry_id)
            assert isinstance(item, dict)
            error = check_revision(item)
            if error:
                unusable[entry_id] = f"the model's answer was unusable: {error}"
            else:
                messages[entry_id] = item["message"]
    for entry_id in ids - seen:
        unusable[entry_id] = "the model gave no answer for it"
    return messages, unusable


def as_written(english: str, answer: str) -> str:
    """An answer as the catalog takes it: NFC, with the English's own leading and
    trailing spaces in place of the model's."""
    lead = english[: len(english) - len(english.lstrip())]
    trail = english[len(english.rstrip()) :]
    return unicodedata.normalize("NFC", f"{lead}{answer.strip()}{trail}")


# --- the run ---------------------------------------------------------------------------


def revise_batches(
    client: Any,
    model: str,
    rates: review.Rates,
    prefix: str,
    batches: list[list[Target]],
    max_usd: float,
    errors: tuple[type[BaseException], ...],
) -> tuple[review.Spend, str | None]:
    """Send batch after batch until done, the cap, or an AWS error; return the spend and
    why the run stopped early, if it did. An answered entry gets ``after``; an entry
    without a usable answer is refused."""
    spend = review.Spend()
    for batch in batches:
        ceiling = ceiling_usd(rates, prefix, batch)
        if spend.usd + ceiling > max_usd:
            if spend.calls == 0:
                raise ToolError(
                    f"the first batch could cost up to US${ceiling:.2f}, over --max-usd "
                    f"{max_usd:.2f}; nothing was sent"
                )
            return spend, (
                f"the next batch could take the run past --max-usd {max_usd:.2f}, "
                f"so it stopped at US${spend.usd:.2f}"
            )
        try:
            response = client.converse(**converse_request(model, prefix, batch))
        except errors as exc:
            if spend.calls == 0:
                raise ToolError(
                    f"Bedrock refused the run, so nothing was written: {exc}"
                ) from None
            return spend, f"Bedrock refused a call, so the run stopped: {exc}"
        spend.calls += 1
        usage = review.usage_of(response)
        if usage is None:
            spend.usd += ceiling  # an unreported usage is counted at its most
        else:
            spend.usd += rates.cost(usage)
            for key, value in usage.items():
                spend.tokens[key] += value
        messages, unusable = read_answer(response, batch)
        for target in batch:
            if target.id in messages:
                target.after = as_written(target.english, messages[target.id])
            else:
                target.outcome = "refused"
                target.reason = unusable[target.id]
    return spend, None


def source_hash(text: str) -> str:
    """The catalog's ``sourceHash`` of an English message, as ``catalog.mjs`` computes
    it: the coverage worker checks it with every answer before anything is written."""
    digest = hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8"))
    return digest.hexdigest()[:16]


def check_answers(layout: review.Layout, locale: str, answered: list[Target]) -> None:
    """Judge every answer with the build's parity check, through the coverage worker
    over a scratch catalog of the answers; set each entry's outcome."""
    if not answered:
        return
    candidates = {
        target.id: {"message": target.after, "sourceHash": source_hash(target.english)}
        for target in answered
    }
    try:
        with tempfile.TemporaryDirectory(prefix="ui-i18n-revise-") as scratch:
            directory = Path(scratch)
            shutil.copyfile(layout.catalog("en"), directory / "en.json")
            (directory / f"{locale}.json").write_text(
                json.dumps(candidates, ensure_ascii=False), encoding="utf-8"
            )
            report = review.call_worker(
                layout, [str(directory), "--json", "--locale", locale]
            )
        coverage = (report.get("locales") or {}).get(locale)
        if not isinstance(coverage, dict):
            raise ToolError(f"the coverage worker reported nothing for {locale}")
    except (OSError, ToolError) as exc:
        # The answers are paid for: the provenance keeps them, and nothing unchecked
        # reaches the catalog.
        for target in answered:
            target.outcome = "refused"
            target.reason = f"the parity check could not run: {exc}"
        return
    rejected = set(coverage.get("invalid") or []) | set(coverage.get("stale") or [])
    problems = [str(problem) for problem in coverage.get("problems") or []]
    for target in answered:
        if target.id in rejected:
            said = [
                problem
                for problem in problems
                if f'"{target.id}"' in problem or f": {target.id}: " in problem
            ]
            target.outcome = "refused"
            target.reason = "the build's parity check refused it: " + (
                "; ".join(said) or "its sourceHash is not the English's"
            )
        elif target.after == target.before:
            target.outcome = "unchanged"
        else:
            target.outcome = "revised"


def counts_of(targets: list[Target]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(OUTCOMES, 0)
    for target in targets:
        counts[target.outcome] += 1
    return counts


def provenance_path(
    layout: review.Layout, args: argparse.Namespace, started: datetime
) -> Path:
    """Where the run's provenance goes: never under ``src/``, which ships."""
    path = args.provenance or (
        layout.repo_root
        / "test-reports"
        / "ui-i18n-revise"
        / f"{args.locale}-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    if path.resolve().is_relative_to((layout.repo_root / "src").resolve()):
        raise ToolError(
            f"--provenance {layout.relative(path)} is under src/, which ships; "
            f"the provenance stays out of it"
        )
    return path


def entry_record(target: Target) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": target.id,
        "english": target.english,
        "before": target.before,
        "after": target.after,
        "outcome": target.outcome,
        "reason": target.reason,
        "flagged": target.flagged,
        "note": target.note or None,
    }
    if target.outcome == "revised":
        record["message"] = target.after  # what the review's --provenance reads
    return record


# --- the command -----------------------------------------------------------------------


def main(argv: list[str] | None = None, *, layout: review.Layout | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--locale", required=True, help="the catalog to revise, e.g. fr-CA"
    )
    parser.add_argument(
        "--region", help="the AWS Region to call Bedrock in (required unless --dry-run)"
    )
    parser.add_argument("--profile", help="the AWS profile to call it with")
    parser.add_argument(
        "--model",
        default=review.DEFAULT_MODEL,
        help=f"a Claude Opus 5.5 inference profile priced in config_library/pricing.yaml (default {review.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--review",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="the report.json of a make ui-i18n-review run: revise its disagreements and the entries it could not review (repeatable)",
    )
    parser.add_argument(
        "--notes",
        type=Path,
        metavar="PATH",
        help="a JSON object of message id to a note: revise those entries too, with the note",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and the estimate; call nothing",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N", help="revise only the first N entries"
    )
    parser.add_argument(
        "--max-usd",
        type=float,
        default=review.DEFAULT_MAX_USD,
        metavar="USD",
        help=f"refuse to start, and stop, before the run could cost more (default {review.DEFAULT_MAX_USD:.0f})",
    )
    parser.add_argument(
        "--provenance",
        type=Path,
        metavar="PATH",
        help="where to write the provenance file (default under test-reports/ui-i18n-revise/)",
    )
    args = parser.parse_args(argv)
    if not args.review and args.notes is None:
        parser.error(
            "name what to revise: --review <report.json>, --notes <file>, or both"
        )
    if args.limit is not None and args.limit < 1:
        parser.error("--limit takes a whole number from 1")
    if not args.max_usd > 0:
        parser.error("--max-usd takes an amount above 0")
    if not args.dry_run and not args.region:
        parser.error(
            "--region is required unless --dry-run (the Region is never guessed)"
        )
    layout = layout or review.Layout(review.REPO_ROOT)

    try:
        return revise_catalog(layout, args)
    except ToolError as exc:
        print(f"❌ {TOOL}: {exc}", file=sys.stderr)
        return 2


def revise_catalog(layout: review.Layout, args: argparse.Namespace) -> int:
    if args.locale not in review.STYLE_RULES or args.locale not in REVISE_RULES:
        raise ToolError(
            f"there are no revision rules for {args.locale}; add them to STYLE_RULES "
            "and REVISE_RULES first"
        )
    if not layout.parser_package.is_dir():
        raise ToolError(
            f"{layout.relative(layout.parser_package)} is missing; run make setup-node first"
        )
    started = review.now()
    out = provenance_path(layout, args, started)
    rates = review.read_rates(layout.pricing, args.model)
    glossary_path = layout.glossary(args.locale)
    glossary = (
        review.read_glossary(glossary_path, args.locale)
        if glossary_path.is_file()
        else None
    )
    if glossary is None:
        print(f"  no {layout.relative(glossary_path)}: revising without a glossary")
    reports = [read_review(path, args.locale) for path in args.review]
    notes = read_notes(args.notes) if args.notes else {}

    plan = plan_revision(layout, args.locale, glossary, reports, notes, args.limit)
    prefix = instructions(args.locale, glossary)
    batches = make_batches(plan.targets)
    estimate = estimate_usd(rates, prefix, batches)
    count = len(plan.targets)
    print(
        f"{args.locale}: {count} entr{'y' if count == 1 else 'ies'} to revise in "
        f"{len(batches)} batch{'' if len(batches) == 1 else 'es'} with {args.model}; "
        f"about US${estimate:.2f} (cap US${args.max_usd:.2f})"
    )
    if plan.deferred:
        print(f"  {plan.deferred} more left for a later run by --limit")
    print(
        f"  left out: {len(plan.invalid)} invalid, {len(plan.stale)} stale, "
        f"{len(plan.changed)} edited after their review"
    )
    if estimate > args.max_usd:
        raise ToolError(
            f"about US${estimate:.2f} is over --max-usd {args.max_usd:.2f}; "
            f"take fewer entries with --limit, or raise the cap"
        )
    if args.dry_run:
        return 0
    if not plan.targets:
        raise ToolError(f"nothing in {args.locale}.json is ready for a revision")

    try:
        errors = review.aws_errors()
    except ImportError as exc:
        raise ToolError(
            f"the AWS SDK is not installed ({exc}); run make setup-venv"
        ) from None
    try:
        client = review.make_client(args.region, args.profile)
    except errors as exc:
        raise ToolError(f"no Bedrock client could be made: {exc}") from None
    spend, stopped = revise_batches(
        client, args.model, rates, prefix, batches, args.max_usd, errors
    )
    check_answers(layout, args.locale, [t for t in plan.targets if t.after is not None])

    counts = counts_of(plan.targets)
    record = {
        "tool": TOOL,
        "locale": args.locale,
        "targetLanguage": args.locale,
        "model": args.model,
        "region": args.region,
        "effort": review.EFFORT,
        "date": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "promptSha256": prompt_sha256(prefix),
        "glossary": None
        if glossary is None
        else {
            "path": layout.relative(glossary.source),
            "sha256": glossary.sha256,
            "terms": len(glossary.rows),
        },
        "reviews": [layout.relative(path) for path in args.review],
        "notes": layout.relative(args.notes) if args.notes else None,
        "cost": {
            "usd": round(spend.usd, 4),
            "maxUsd": args.max_usd,
            "calls": spend.calls,
            **spend.tokens,
        },
        "stopped": stopped,
        "counts": counts,
        "invalid": plan.invalid,
        "stale": plan.stale,
        "editedAfterReview": plan.changed,
        "entries": [entry_record(target) for target in plan.targets],
    }
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise ToolError(
            f"the provenance file could not be written, so the catalog was not: {exc}"
        ) from None

    revised = [target for target in plan.targets if target.outcome == "revised"]
    if revised:
        path = layout.catalog(args.locale)
        catalog = review.read_json(path)
        for target in revised:
            catalog[target.id] = {
                "message": target.after,
                "sourceHash": source_hash(target.english),
            }
        write_catalog(path, catalog)
    print(
        f"✅ {counts['revised']} revised, {counts['unchanged']} unchanged, "
        f"{counts['refused']} refused, {counts['skipped']} skipped of {count}; "
        f"US${spend.usd:.2f} in {spend.calls} call{'' if spend.calls == 1 else 's'}; "
        f"provenance in {layout.relative(out)}"
    )
    for target in plan.targets:
        if target.outcome == "refused":
            print(f"  refused: {target.id}: {target.reason}")
    if stopped:
        print(f"  the run stopped early: {stopped}")
    return 1 if stopped or counts["refused"] or counts["skipped"] else 0


if __name__ == "__main__":
    sys.exit(main())
