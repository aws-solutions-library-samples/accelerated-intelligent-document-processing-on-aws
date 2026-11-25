#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Review a web UI translation catalog with a Bedrock model, and write what it found.

``make ui-i18n-review LOCALE=fr-CA`` sends every entry of
``src/ui/src/i18n/<locale>.json`` that is valid and current to a Bedrock model, in
batches, and writes a report. It changes no catalog: a person reads the report and
decides. Issue #1770.

WHAT CODE CHECKS, NOT THE MODEL
-------------------------------
The coverage worker (``src/ui/scripts/i18n/coverage.mjs``, which applies the UI
build's own parity check) sorts the catalog first. Entries the build rejects
(``invalid``: an argument lost or renamed, a plural changed, a tag missing) and
entries whose English changed since they were translated (``stale``: translate them
again first) are listed in the report and never sent. So the model is never asked
about arguments, plurals or tags, and spacing before punctuation, which the
translation tool applies by rule, is left out of its brief too.

WHAT THE MODEL IS ASKED
-----------------------
Per entry: the English, the developer's description of where it appears, the
current translation and the glossary terms its English uses. The instructions, the
locale's style rules (``STYLE_RULES``) and the whole glossary
(``src/ui/src/i18n/glossary.<locale>.csv``) open every request and end in a cache
point, so after the first batch they are billed at the cache-read rate. The model
answers through one tool (``TOOL_NAME``) with, per entry, the problems it sees,
each from a fixed list (``PROBLEMS``), a short reason and its own translation.

ONE TRANSLATION RUN
-------------------
``--provenance PATH`` takes the provenance file a ``make ui-i18n-translate`` run
wrote (under ``test-reports/ui-i18n-translate/``) and reviews only the entries that
run wrote. Each carries that run's back-translation, when it was asked for, as
evidence: a back-translation whose meaning differs from the English points at a
meaning problem, though the model judges the translation itself. The provenance file
of a ``make ui-i18n-revise`` run is read the same way, and carries no back-translation.

THE ANSWER IS DATA
------------------
An answer is checked before anything in it is counted: it must come through the
tool, name only ids of its own batch, each once, with problems from the list and
strings of bounded length. An entry whose answer fails any of that, and every entry
of a batch answered in prose or cut off at the token limit, is listed as unreviewed
with the reason.

THE AGREEMENT LOG
-----------------
Each reviewed entry lands in one class:

    same          no problem, and the model's translation is the current one
                  (spacing and apostrophe style aside)
    same-meaning  no problem, worded otherwise: the current translation stands
    disagreement  at least one problem: a person decides

The agreement figure is (same + same-meaning) / reviewed. A suggestion a person
takes is copied into the catalog by hand, where the catalog gate checks it like
any edit.

WHAT IT COSTS
-------------
Tokens are priced from ``config_library/pricing.yaml`` for the model, and a model
with no entry there is refused. Before any call the run estimates its cost and
refuses to start above ``--max-usd`` (US$20 by default). During the run it stops
before any batch that could take it past the cap, counting that batch at its
largest possible size, every retry included. An answered call adds the usage it
reports, which is its last attempt's, and each attempt botocore retried before it
(``RetryAttempts``) at the most one attempt can cost: botocore retries a read timeout
too, and a timed-out attempt may be billed. A call that fails after its retries adds
its batch at its largest. The report shows that part on a line of its own, since a
throttled attempt is not billed. ``--dry-run`` prints the plan and the
estimate and calls nothing; ``--limit N`` takes the first N entries, for a small
trial. It runs with the developer's own AWS credentials (``--profile``, or the usual
AWS environment) in the Region named by ``--region``; nothing in a deployed stack
calls it.

THE REQUEST
-----------
Shaped for Claude Opus 5.5 on the Converse API, through any of its inference
profiles: no temperature, which it rejects; reasoning effort ``low``, because its
thinking cannot be turned off and is billed as output; and ``toolChoice`` ``auto``,
because it rejects a forced tool, so the instructions ask for the tool instead.

The report, ``report.json`` and ``report.md``, goes under the gitignored
``test-reports/ui-i18n-review/``, never under ``src/``.

Exit codes:
    0 — the report covers every entry it was asked to review, whatever it found, or a
        dry run whose estimate is within the cap
    1 — the report is partial: the cap or an AWS error stopped the run, or some
        answers could not be used (each is listed)
    2 — no report: usage, a missing toolchain or catalog, a model with no price, an
        estimate over the cap, or an AWS error before any entry was reviewed

USAGE
-----
    python3 scripts/ui_i18n_review.py --locale fr-CA --dry-run
    python3 scripts/ui_i18n_review.py --locale fr-CA --region ca-central-1 --profile <name> --limit 40
    python3 scripts/ui_i18n_review.py --locale fr-CA --region ca-central-1 --profile <name> \\
        --provenance test-reports/ui-i18n-translate/fr-CA-<stamp>.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import re
import subprocess
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

TOOL = "ui-i18n-review"

#: The model the review runs on unless --model names another inference profile of it.
DEFAULT_MODEL = "us.anthropic.claude-opus-5-5"

#: The default cap on one run's cost, in US dollars.
DEFAULT_MAX_USD = 20.0

#: Reasoning effort. Thinking cannot be turned off on Claude Opus 5.5 and is billed as
#: output, so the lowest level keeps a full pass well inside the cap.
EFFORT = "low"

#: The most entries, and the most characters of entry text, one request carries.
BATCH_ENTRIES = 40
BATCH_CHARS = 12_000

#: The most output tokens, thinking included, one request may produce.
MAX_OUTPUT_TOKENS = 16_000

#: How many times one request may be sent: botocore retries a throttle or a timeout.
ATTEMPTS = 3

#: For the estimate before the run: characters per token, and output tokens per entry.
CHARS_PER_TOKEN = 3.5
OUTPUT_TOKENS_PER_ENTRY = 150

#: For the cap during the run: the fewest characters one token stands for, so a
#: batch is never counted at less than it can cost.
FEWEST_CHARS_PER_TOKEN = 2

#: The one tool the model answers through.
TOOL_NAME = "record_review"

#: The problems the model may report, and what each means.
PROBLEMS = {
    "meaning": "says something the English does not, or leaves part of it out",
    "glossary": "does not use the glossary's term for an English term, or misuses it",
    "register": "not the register the rules ask for, or a tone wrong for a user interface",
    "regional": "the usage of another region where the locale's own usage exists",
    "grammar": "spelling, agreement or conjugation",
    "typography": "apostrophes, quotation marks or capitals",
}

#: The longest reason kept (a longer one is cut) and the longest translation accepted.
MAX_REASON_CHARS = 500
MAX_RENDERING_CHARS = 4_000

#: Each locale's review rules. A locale with none cannot be reviewed.
STYLE_RULES = {
    "fr-CA": (
        "- The formal register: vous, never tu.\n"
        "- Quebec usage first (OQLF Grand dictionnaire terminologique, Termium Plus), "
        "for example téléverser for upload, courriel for e-mail, clavardage for chat. "
        "France usage where a Quebec usage exists is a regional problem.\n"
        "- The glossary is binding: its French term for its English term.\n"
        "- Assign is attribuer, with its forms attribué (agreeing in gender and number), "
        "attribution, réattribuer, désattribuer and non attribué; never assigner, "
        "affecter or assignation.\n"
        "- AWS and other product names stay as they are; descriptive feature names are "
        "translated as the glossary says.\n"
        "- The typographic apostrophe ’, and « » quotation marks.\n"
        "- Sentence case in titles, labels and buttons.\n"
        "- The French plural one covers 0 and 1."
    ),
}

#: Indirection for the tests, which replace it rather than running node.
run = subprocess.run


class ToolError(Exception):
    """No report can be written: the message says why."""


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
        return self.ui_root / "scripts" / "i18n" / "coverage.mjs"

    @property
    def parser_package(self) -> Path:
        """The installed package the worker's parity check parses ICU with."""
        return self.ui_root / "node_modules" / "@formatjs" / "icu-messageformat-parser"

    @property
    def pricing(self) -> Path:
        return self.repo_root / "config_library" / "pricing.yaml"

    @property
    def reports_dir(self) -> Path:
        return self.repo_root / "test-reports" / "ui-i18n-review"

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


def call_worker(layout: Layout, args: list[str]) -> Any:
    """Run the coverage worker with ``args``; return its JSON answer."""
    command = ["node", str(layout.worker), *args]
    try:
        result = run(
            command, cwd=layout.ui_root, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise ToolError(f"node could not be started: {exc}") from None
    if result.returncode != 0:
        raise ToolError(
            f"the coverage worker exited {result.returncode}: "
            f"{result.stderr.strip() or 'no message'}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError(f"the coverage worker printed no JSON ({exc})") from None


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"{path.name} could not be read: {exc}") from None
    if not isinstance(data, dict):
        raise ToolError(f"{path.name} is not a JSON object keyed by message id")
    return data


# --- the glossary and the price ------------------------------------------------------


@dataclass(frozen=True)
class Glossary:
    """The glossary's rows, note included: the model reads all three columns."""

    rows: tuple[tuple[str, str, str], ...]
    source: Path
    sha256: str

    def terms_in(self, english: str) -> list[str]:
        """The English terms that occur in ``english`` as whole words, any case."""
        return [
            term
            for term, _, _ in self.rows
            if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", english, re.IGNORECASE)
        ]

    def as_csv(self, locale: str) -> str:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["en", locale, "note"])
        writer.writerows(self.rows)
        return out.getvalue()


def read_glossary(path: Path, locale: str) -> Glossary:
    """All three columns. Refuses a header other than ``en,<locale>,…``, an empty
    term, or no terms at all. A byte-order mark (a spreadsheet's export) is ignored."""
    data = path.read_bytes()
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    if not rows or rows[0][:2] != ["en", locale]:
        raise ToolError(f"{path.name}: the header must start with en,{locale}")
    terms = []
    for number, row in enumerate(rows[1:], start=2):
        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            raise ToolError(
                f"{path.name}:{number}: a term needs its English and its {locale}"
            )
        terms.append((row[0], row[1], row[2] if len(row) > 2 else ""))
    if not terms:
        raise ToolError(f"{path.name}: the glossary has no terms")
    return Glossary(
        rows=tuple(terms), source=path, sha256=hashlib.sha256(data).hexdigest()
    )


@dataclass(frozen=True)
class Rates:
    """US dollars per token, from ``config_library/pricing.yaml``."""

    input: float
    output: float
    cache_read: float
    cache_write: float

    def cost(self, usage: Mapping[str, int]) -> float:
        return (
            usage.get("inputTokens", 0) * self.input
            + usage.get("outputTokens", 0) * self.output
            + usage.get("cacheReadInputTokens", 0) * self.cache_read
            + usage.get("cacheWriteInputTokens", 0) * self.cache_write
        )


#: The usage counts a Converse response reports, as the report names them.
USAGE_KEYS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheWriteInputTokens",
)


def read_rates(path: Path, model: str) -> Rates:
    """The model's rates. Refuses a model with no entry: a run it cannot price, it cannot cap."""
    import yaml

    try:
        pricing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ToolError(f"{path.name} could not be read: {exc}") from None
    for entry in pricing.get("pricing") or []:
        if not isinstance(entry, dict) or entry.get("name") != f"bedrock/{model}":
            continue
        units = {
            unit.get("name"): unit.get("price")
            for unit in entry.get("units") or []
            if isinstance(unit, dict)
        }
        try:
            return Rates(
                input=float(units["inputTokens"]),
                output=float(units["outputTokens"]),
                cache_read=float(units["cacheReadInputTokens"]),
                cache_write=float(units["cacheWriteInputTokens"]),
            )
        except (KeyError, TypeError, ValueError):
            raise ToolError(
                f"bedrock/{model} in {path.name} lacks a token rate, so the run cannot be capped"
            ) from None
    raise ToolError(
        f"{model} has no entry in {path.name}, so the run cannot be priced or capped"
    )


# --- the plan --------------------------------------------------------------------------


@dataclass
class Entry:
    """One catalog entry under review, and what the review made of it."""

    id: str
    english: str
    description: str
    translation: str
    terms: list[str]
    back_translation: list[str] = field(default_factory=list)
    outcome: str = "skipped"
    problems: list[str] = field(default_factory=list)
    reason: str = ""
    rendering: str = ""

    def request(self) -> dict[str, Any]:
        """The entry as the model receives it."""
        item: dict[str, Any] = {
            "id": self.id,
            "english": self.english,
            "description": self.description,
            "translation": self.translation,
        }
        if self.terms:
            item["glossaryTerms"] = self.terms
        if self.back_translation:
            item["backTranslation"] = self.back_translation
        return item

    @property
    def size(self) -> int:
        return len(json.dumps(self.request(), ensure_ascii=False))


@dataclass
class Plan:
    locale: str
    entries: list[Entry]
    deferred: int
    invalid: list[str]
    problems: list[str]
    stale: list[str]
    translated: int
    not_in_catalog: int = 0


@dataclass(frozen=True)
class Provenance:
    """What one run of the translation tool (``make ui-i18n-translate``) recorded: the
    entries it wrote, and the back-translations it was asked for."""

    source: Path
    date: str | None
    terminology_sha256: str | None
    #: The message that run wrote, per entry.
    written: dict[str, str]
    #: One text per plural or select branch, only for entries with every branch's.
    back_translations: dict[str, list[str]]

    def back_translation(self, entry_id: str, translation: str) -> list[str]:
        """The run's back-translation, if the catalog still holds what that run wrote: an
        entry edited since is judged without evidence about its older text."""
        if self.written.get(entry_id) != translation:
            return []
        return self.back_translations.get(entry_id, [])


def branch_texts(sentences: Any) -> list[str] | None:
    """One back-translation per branch, in order, or None when any branch lacks one (the
    translation tool records none when its cap or an AWS error stopped it), so that a
    text is never read against the wrong branch. A branch with nothing to translate,
    such as a bare ``#``, was never sent and stands for itself."""
    if not isinstance(sentences, list) or not sentences:
        return None
    texts = []
    for sentence in sentences:
        if not isinstance(sentence, dict):
            return None
        if sentence.get("sent") is None and isinstance(sentence.get("answer"), str):
            texts.append(plain_text(sentence["answer"]))
        elif isinstance(sentence.get("backTranslation"), str):
            texts.append(plain_text(sentence["backTranslation"]))
        else:
            return None
    return texts


def plain_text(answer: str) -> str:
    """A back-translation as text: the translation tool sends and receives HTML, with
    every argument in a ``<span>``."""
    return html.unescape(re.sub(r"</?span\b[^>]*>", "", answer))


def read_provenance(path: Path, locale: str) -> Provenance:
    """Refuses a file that is not a translation run's provenance for ``locale``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"{path.name} could not be read: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ToolError(f"{path.name} is not the provenance file of a translation run")
    if data.get("targetLanguage") != locale:
        raise ToolError(
            f"{path.name} records a run for {data.get('targetLanguage')!r}, not {locale}"
        )
    written: dict[str, str] = {}
    back_translations: dict[str, list[str]] = {}
    for entry in data["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ToolError(f"{path.name} holds an entry without an id")
        if not isinstance(entry.get("message"), str):
            continue  # that run left it out, so the screen shows the English
        written[entry["id"]] = entry["message"]
        texts = branch_texts(entry.get("sentences"))
        if texts:
            back_translations[entry["id"]] = texts
    terminology = data.get("terminology")
    return Provenance(
        source=path,
        date=data.get("date") if isinstance(data.get("date"), str) else None,
        terminology_sha256=terminology.get("sha256")
        if isinstance(terminology, dict)
        else None,
        written=written,
        back_translations=back_translations,
    )


def plan_review(
    layout: Layout,
    locale: str,
    glossary: Glossary | None,
    limit: int | None,
    provenance: Provenance | None = None,
) -> Plan:
    """The entries to send: every entry of ``<locale>.json`` the build accepts and whose
    English has not changed since it was translated, in id order; with a provenance
    file, only the entries that translation run wrote, with its back-translations."""
    path = layout.catalog(locale)
    if not path.is_file():
        raise ToolError(
            f"there is no {layout.relative(path)} to review; draft one with make ui-i18n-translate"
        )
    english = read_json(layout.catalog("en"))
    catalog = read_json(path)
    report = call_worker(
        layout, [str(layout.catalog_dir), "--json", "--locale", locale]
    )
    coverage = (report.get("locales") or {}).get(locale)
    if not isinstance(coverage, dict):
        raise ToolError(f"the coverage worker reported nothing for {locale}")
    invalid = sorted(coverage.get("invalid") or [])
    stale = sorted(coverage.get("stale") or [])
    left_out = set(invalid) | set(stale)
    entries = []
    for entry_id in sorted(catalog):
        source, translated = english.get(entry_id), catalog[entry_id]
        if entry_id in left_out or not isinstance(source, dict):
            continue
        if provenance is not None and entry_id not in provenance.written:
            continue
        message = source["defaultMessage"]
        entries.append(
            Entry(
                id=entry_id,
                english=message,
                description=str(source.get("description") or ""),
                translation=translated["message"],
                terms=glossary.terms_in(message) if glossary else [],
                back_translation=provenance.back_translation(
                    entry_id, translated["message"]
                )
                if provenance
                else [],
            )
        )
    kept = entries if limit is None else entries[:limit]
    return Plan(
        locale=locale,
        entries=kept,
        deferred=len(entries) - len(kept),
        invalid=invalid,
        problems=list(coverage.get("problems") or []),
        stale=stale,
        translated=len(entries),
        not_in_catalog=len(set(provenance.written) - set(catalog)) if provenance else 0,
    )


def make_batches(entries: list[Entry]) -> list[list[Entry]]:
    """At most ``BATCH_ENTRIES`` entries and ``BATCH_CHARS`` characters per batch, in order;
    an entry larger than the limit goes alone."""
    batches: list[list[Entry]] = []
    current: list[Entry] = []
    chars = 0
    for entry in entries:
        if current and (
            len(current) == BATCH_ENTRIES or chars + entry.size > BATCH_CHARS
        ):
            batches.append(current)
            current, chars = [], 0
        current.append(entry)
        chars += entry.size
    if current:
        batches.append(current)
    return batches


# --- the request -----------------------------------------------------------------------

#: The tool's input schema: what an answer must look like.
TOOL_SPEC = {
    "name": TOOL_NAME,
    "description": "Record the review of every message you were given, once each.",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "reviews": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "problems": {
                                "type": "array",
                                "items": {"type": "string", "enum": list(PROBLEMS)},
                            },
                            "reason": {"type": "string"},
                            "rendering": {"type": "string"},
                        },
                        "required": ["id", "problems", "reason", "rendering"],
                    },
                }
            },
            "required": ["reviews"],
        }
    },
}


def instructions(locale: str, glossary: Glossary | None) -> str:
    """The cached prefix of every request: the task, the rules and the glossary."""
    problems = "\n".join(f"- {name}: {meaning}" for name, meaning in PROBLEMS.items())
    text = (
        f"You review {locale} translations of the user interface of a web application, "
        "the GenAI Intelligent Document Processing accelerator: people upload documents, "
        "check the data extracted from them in review queues, test and evaluate "
        "configurations, and chat with agents.\n\n"
        "Each message you are given has an id, the English source in ICU MessageFormat, "
        "a description by the developer of where it appears, the current translation, "
        "and the glossary terms its English uses. Some also carry backTranslation: the "
        "translation machine-translated back into English, one text per plural or select "
        "branch. It is evidence only: a back-translation whose meaning differs from the "
        "English points at a meaning problem, but judge the translation itself.\n\n"
        "For each message:\n"
        "1. Judge the current translation against the English and the rules below. List "
        "each problem you find as one of these names, or none when the translation is "
        "acceptable as it is. A matter of taste is not a problem.\n"
        f"{problems}\n"
        "2. Write your own translation of the English, following the same rules, in ICU "
        "MessageFormat.\n"
        "3. In reason, say in one or two short sentences, in English, what is wrong, or ok.\n\n"
        "Arguments in braces ({name}, {count, plural, ...}), # inside a plural, and tags "
        "such as <b>...</b> are checked by code: keep every one of them unchanged in your "
        "translation, and never list a problem about them. Spacing before punctuation is "
        "applied by code and may not show in what you read: never list a problem about it.\n\n"
        f"Rules for {locale}:\n{STYLE_RULES[locale]}\n\n"
        f"Answer by calling the {TOOL_NAME} tool once, with one review per message id you "
        "were given, and write nothing else."
    )
    if glossary is not None:
        text += (
            f"\n\nGlossary (English term, {locale} term, note for reviewers):\n"
            + glossary.as_csv(locale)
        )
    return text


def batch_text(batch: list[Entry]) -> str:
    items = [entry.request() for entry in batch]
    return f"Review these {len(batch)} messages:\n" + json.dumps(
        items, ensure_ascii=False
    )


def converse_request(model: str, prefix: str, batch: list[Entry]) -> dict[str, Any]:
    return {
        "modelId": model,
        "system": [{"text": prefix}, {"cachePoint": {"type": "default"}}],
        "messages": [{"role": "user", "content": [{"text": batch_text(batch)}]}],
        "toolConfig": {"tools": [{"toolSpec": TOOL_SPEC}], "toolChoice": {"auto": {}}},
        "inferenceConfig": {"maxTokens": MAX_OUTPUT_TOKENS},
        "additionalModelRequestFields": {"output_config": {"effort": EFFORT}},
    }


def prefix_chars(prefix: str) -> int:
    """The characters every request opens with: the instructions and the tool's schema."""
    return len(prefix) + len(json.dumps(TOOL_SPEC))


def estimate_usd(rates: Rates, prefix: str, batches: list[list[Entry]]) -> float:
    """The expected cost: the prefix written to the cache once and read after, the
    entries at the input rate, and ``OUTPUT_TOKENS_PER_ENTRY`` per entry."""
    total = 0.0
    shared = prefix_chars(prefix) / CHARS_PER_TOKEN
    for index, batch in enumerate(batches):
        total += shared * (rates.cache_write if index == 0 else rates.cache_read)
        total += len(batch_text(batch)) / CHARS_PER_TOKEN * rates.input
        total += len(batch) * OUTPUT_TOKENS_PER_ENTRY * rates.output
    return total


def ceiling_usd(rates: Rates, prefix: str, batch: list[Entry]) -> float:
    """The most one batch can cost: every input token at the dearest input rate, the
    output at its limit, every attempt botocore may make."""
    tokens = (prefix_chars(prefix) + len(batch_text(batch))) / FEWEST_CHARS_PER_TOKEN
    one = tokens * max(rates.input, rates.cache_write)
    return ATTEMPTS * (one + MAX_OUTPUT_TOKENS * rates.output)


# --- the answer ------------------------------------------------------------------------


def check_review(item: dict[str, Any]) -> str | None:
    """Why one review cannot be used, or None."""
    problems = item.get("problems")
    if not isinstance(problems, list) or not all(isinstance(p, str) for p in problems):
        return "its problems are not a list of names"
    unknown = sorted(set(problems) - set(PROBLEMS))
    if unknown:
        return f"it names problems outside the list: {', '.join(unknown)}"
    if len(set(problems)) != len(problems):
        return "it names a problem twice"
    if not isinstance(item.get("reason"), str):
        return "its reason is not text"
    rendering = item.get("rendering")
    if not isinstance(rendering, str) or not rendering.strip():
        return "it has no translation of its own"
    if len(rendering) > MAX_RENDERING_CHARS:
        return f"its translation is longer than {MAX_RENDERING_CHARS} characters"
    return None


def read_answer(
    response: Mapping[str, Any], batch: list[Entry]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """The usable reviews by id, and why each other id of the batch has none."""
    ids = {entry.id for entry in batch}
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
    reviews: dict[str, dict[str, Any]] = {}
    unusable: dict[str, str] = {}
    seen: set[str] = set()
    for call in calls:
        arguments = call.get("input")
        items = arguments.get("reviews") if isinstance(arguments, dict) else None
        for item in items if isinstance(items, list) else []:
            entry_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(entry_id, str) or entry_id not in ids:
                continue  # an answer for no entry of this batch counts for nothing
            if entry_id in seen:
                reviews.pop(entry_id, None)
                unusable[entry_id] = "the model answered for it twice"
                continue
            seen.add(entry_id)
            assert isinstance(item, dict)
            error = check_review(item)
            if error:
                unusable[entry_id] = f"the model's answer was unusable: {error}"
            else:
                reviews[entry_id] = item
    for entry_id in ids - seen:
        unusable[entry_id] = "the model gave no answer for it"
    return reviews, unusable


_SPACES = "[\\s\u00a0\u202f]"


def comparable(text: str) -> str:
    """``text`` with the differences the comparison ignores taken out: Unicode form,
    apostrophe style, spacing around punctuation, runs of spaces."""
    text = unicodedata.normalize("NFC", text).replace("'", "’")
    text = re.sub(f"{_SPACES}+(?=[:;?!»])", "", text)
    text = re.sub(f"(?<=«){_SPACES}+", "", text)
    return re.sub(f"{_SPACES}+", " ", text).strip()


def classify(entry: Entry, review: Mapping[str, Any]) -> None:
    """Record one usable review on its entry, with the entry's agreement class."""
    entry.problems = list(review["problems"])
    entry.reason = review["reason"][:MAX_REASON_CHARS]
    entry.rendering = review["rendering"]
    if entry.problems:
        entry.outcome = "disagreement"
    elif comparable(entry.rendering) == comparable(entry.translation):
        entry.outcome = "same"
    else:
        entry.outcome = "same-meaning"


# --- the run ---------------------------------------------------------------------------


def make_client(region: str, profile: str | None) -> Any:
    """A bedrock-runtime client for the developer's own credentials. Built only when a run
    calls AWS. A review batch is answered in one non-streaming response, so the read
    timeout is long, and the attempt count is the one the cap counts."""
    import boto3
    from botocore.config import Config

    config = Config(
        connect_timeout=10,
        read_timeout=900,
        retries={"total_max_attempts": ATTEMPTS, "mode": "standard"},
    )
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("bedrock-runtime", config=config)


def aws_errors() -> tuple[type[BaseException], ...]:
    """What a failed AWS call raises; imported late so a dry run needs no AWS library."""
    from botocore.exceptions import BotoCoreError, ClientError

    return (BotoCoreError, ClientError)


def now() -> datetime:
    """The run's timestamp; a function so the tests can fix it."""
    return datetime.now(timezone.utc)


def usage_of(response: Mapping[str, Any]) -> dict[str, int] | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    counts = {key: usage.get(key, 0) for key in USAGE_KEYS}
    if not all(isinstance(value, int) and value >= 0 for value in counts.values()):
        return None
    return counts


def retries_of(response: Mapping[str, Any]) -> int:
    """The attempts botocore retried before ``response`` (``RetryAttempts`` in its
    ``ResponseMetadata``); every retry it may make when the response does not say."""
    metadata = response.get("ResponseMetadata")
    retries = metadata.get("RetryAttempts") if isinstance(metadata, dict) else None
    if not isinstance(retries, int) or retries < 0:
        return ATTEMPTS - 1
    return retries


@dataclass
class Spend:
    usd: float = 0.0
    calls: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: dict.fromkeys(USAGE_KEYS, 0))
    #: Of ``usd``, what attempts with no usage of their own add at their most: the
    #: attempts botocore retried before an answer, and every attempt of a call that failed.
    retried_usd: float = 0.0
    retried: int = 0


def count_call(
    spend: Spend, rates: Rates, ceiling: float, response: Mapping[str, Any] | None
) -> None:
    """Add one call to ``spend``. ``ceiling`` is the most its batch can cost, every attempt
    included (``ceiling_usd``), so ``ceiling / ATTEMPTS`` is the most one attempt can cost.

    An answered call adds the usage it reports, or the whole ceiling when it reports none.
    That usage is its last attempt's: each attempt botocore retried before it reported none,
    and a retried read timeout may have been billed, so each adds the most one attempt can
    cost, as an unreported usage does. A call that failed after its retries (``response``
    None) adds the whole ceiling."""
    if response is None:
        spend.usd += ceiling
        spend.retried_usd += ceiling
        spend.retried += ATTEMPTS
        return
    spend.calls += 1
    usage = usage_of(response)
    if usage is None:
        spend.usd += ceiling  # an unreported usage is counted at its most
        return
    spend.usd += rates.cost(usage)
    for key, value in usage.items():
        spend.tokens[key] += value
    retries = retries_of(response)
    spend.usd += retries * ceiling / ATTEMPTS
    spend.retried_usd += retries * ceiling / ATTEMPTS
    spend.retried += retries


def retried_note(usd: float, attempts: int) -> str:
    """The line that shows, apart, what retried and failed attempts add to the cost."""
    return (
        f"US${usd:.2f} of the cost counts {attempts} retried or failed "
        f"attempt{'' if attempts == 1 else 's'} at their most: a throttled attempt "
        "is not billed"
    )


def review_batches(
    client: Any,
    model: str,
    rates: Rates,
    prefix: str,
    batches: list[list[Entry]],
    max_usd: float,
    errors: tuple[type[BaseException], ...],
) -> tuple[Spend, str | None]:
    """Review batch after batch until done, the cap, or an AWS error; return the spend and
    why the run stopped early, if it did."""
    spend = Spend()
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
            count_call(spend, rates, ceiling, None)
            if spend.calls == 0:
                raise ToolError(
                    f"Bedrock refused the run, so no report was written (its attempts "
                    f"count up to US${spend.usd:.2f}): {exc}"
                ) from None
            return spend, f"Bedrock refused a call, so the run stopped: {exc}"
        count_call(spend, rates, ceiling, response)
        reviews, unusable = read_answer(response, batch)
        for entry in batch:
            if entry.id in reviews:
                classify(entry, reviews[entry.id])
            else:
                entry.outcome = "unreviewed"
                entry.reason = unusable[entry.id]
    return spend, None


# --- the report ------------------------------------------------------------------------

OUTCOMES = ("same", "same-meaning", "disagreement", "unreviewed", "skipped")


def counts_of(entries: list[Entry]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(OUTCOMES, 0)
    for entry in entries:
        counts[entry.outcome] += 1
    return counts


def agreement_percent(counts: Mapping[str, int]) -> float | None:
    reviewed = counts["same"] + counts["same-meaning"] + counts["disagreement"]
    if reviewed == 0:
        return None
    return round((counts["same"] + counts["same-meaning"]) / reviewed * 100, 1)


def cell(text: str) -> str:
    """``text`` safe inside one Markdown table cell."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def report_markdown(record: dict[str, Any]) -> str:
    counts = record["counts"]
    percent = record["agreementPercent"]
    lines = [
        f"# {record['locale']} translation review",
        "",
        f"{record['model']} in {record['region']}, {record['date']}. "
        f"{record['reviewed']} of {record['translated']} current entries reviewed; "
        f"agreement {'n/a' if percent is None else f'{percent}%'} "
        f"({counts['same']} same, {counts['same-meaning']} same meaning, "
        f"{counts['disagreement']} disagreements). "
        f"Cost US${record['cost']['usd']:.2f} of the US${record['cost']['maxUsd']:.2f} cap.",
    ]
    if record["cost"]["retriedAttempts"]:
        cost = record["cost"]
        lines += ["", f"{retried_note(cost['retriedUsd'], cost['retriedAttempts'])}."]
    provenance = record.get("provenance")
    if provenance:
        lines += [
            "",
            f"Only the entries written by the translation run of {provenance['date']} "
            f"(`{cell(provenance['path'])}`).",
        ]
    if record["stopped"]:
        lines += ["", f"**The run stopped early:** {record['stopped']}."]
    disagreements = [e for e in record["entries"] if e["outcome"] == "disagreement"]
    lines += ["", "## Disagreements: a person decides", ""]
    if disagreements:
        lines += [
            "| Id | English | Current | Suggested | Problems | Reason |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{cell(e['id'])}` | {cell(e['english'])} | {cell(e['translation'])} | "
            f"{cell(e['rendering'])} | {', '.join(e['problems'])} | {cell(e['reason'])} |"
            for e in disagreements
        ]
    else:
        lines.append("None.")
    left = [
        ("Unreviewed: the answer could not be used", "unreviewed"),
        ("Skipped: the run stopped before them", "skipped"),
    ]
    lines += ["", "## Not reviewed", ""]
    lines += [f"- Invalid, the build rejects them: {len(record['invalid'])}"]
    lines += [f"  - `{cell(problem)}`" for problem in record["invalidProblems"]]
    lines += [f"- Stale, translate them again first: {len(record['stale'])}"]
    lines += [f"  - `{cell(entry_id)}`" for entry_id in record["stale"]]
    for title, outcome in left:
        chosen = [e for e in record["entries"] if e["outcome"] == outcome]
        lines.append(f"- {title}: {len(chosen)}")
        lines += [
            f"  - `{cell(e['id'])}`" + (f": {cell(e['reason'])}" if e["reason"] else "")
            for e in chosen
        ]
    return "\n".join(lines) + "\n"


def report_dir(layout: Layout, args: argparse.Namespace, started: datetime) -> Path:
    """Where the report goes: never under ``src/``, which ships."""
    path = args.out or layout.reports_dir / (
        f"{args.locale}-{started.strftime('%Y%m%dT%H%M%SZ')}"
    )
    if path.resolve().is_relative_to((layout.repo_root / "src").resolve()):
        raise ToolError(
            f"--out {layout.relative(path)} is under src/, which ships; the report stays out of it"
        )
    return path


def write_report(directory: Path, record: dict[str, Any]) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "report.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (directory / "report.md").write_text(report_markdown(record), encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"the report could not be written: {exc}") from None


# --- the command -----------------------------------------------------------------------


def main(argv: list[str] | None = None, *, layout: Layout | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--locale", required=True, help="the catalog to review, e.g. fr-CA"
    )
    parser.add_argument(
        "--region", help="the AWS Region to call Bedrock in (required unless --dry-run)"
    )
    parser.add_argument("--profile", help="the AWS profile to call it with")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"a Claude Opus 5.5 inference profile priced in config_library/pricing.yaml (default {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and the estimate; call nothing",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N", help="review only the first N entries"
    )
    parser.add_argument(
        "--max-usd",
        type=float,
        default=DEFAULT_MAX_USD,
        metavar="USD",
        help=f"refuse to start, and stop, before the run could cost more (default {DEFAULT_MAX_USD:.0f})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        metavar="DIR",
        help="where to write report.json and report.md (default under test-reports/ui-i18n-review/)",
    )
    parser.add_argument(
        "--provenance",
        type=Path,
        metavar="PATH",
        help="review only the entries one make ui-i18n-translate or make ui-i18n-revise run wrote, with its back-translations",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit takes a whole number from 1")
    if not args.max_usd > 0:
        parser.error("--max-usd takes an amount above 0")
    if not args.dry_run and not args.region:
        parser.error(
            "--region is required unless --dry-run (the Region is never guessed)"
        )
    layout = layout or Layout(REPO_ROOT)

    try:
        return review_catalog(layout, args)
    except ToolError as exc:
        print(f"❌ {TOOL}: {exc}", file=sys.stderr)
        return 2


def review_catalog(layout: Layout, args: argparse.Namespace) -> int:
    if args.locale not in STYLE_RULES:
        raise ToolError(
            f"there are no review rules for {args.locale}; add them to STYLE_RULES first"
        )
    if not layout.parser_package.is_dir():
        raise ToolError(
            f"{layout.relative(layout.parser_package)} is missing; run make setup-node first"
        )
    started = now()
    out = report_dir(layout, args, started)
    rates = read_rates(layout.pricing, args.model)
    glossary_path = layout.glossary(args.locale)
    glossary = (
        read_glossary(glossary_path, args.locale) if glossary_path.is_file() else None
    )
    if glossary is None:
        print(f"  no {layout.relative(glossary_path)}: reviewing without a glossary")
    provenance = (
        read_provenance(args.provenance, args.locale) if args.provenance else None
    )

    plan = plan_review(layout, args.locale, glossary, args.limit, provenance)
    if plan.not_in_catalog:
        print(
            f"  {plan.not_in_catalog} entr{'y' if plan.not_in_catalog == 1 else 'ies'} "
            f"that run wrote {'is' if plan.not_in_catalog == 1 else 'are'} no longer in "
            f"{args.locale}.json"
        )
    prefix = instructions(args.locale, glossary)
    batches = make_batches(plan.entries)
    estimate = estimate_usd(rates, prefix, batches)
    print(
        f"{args.locale}: {len(plan.entries)} entr{'y' if len(plan.entries) == 1 else 'ies'} "
        f"to review in {len(batches)} batch{'' if len(batches) == 1 else 'es'} with {args.model}; "
        f"about US${estimate:.2f} (cap US${args.max_usd:.2f})"
    )
    if plan.deferred:
        print(f"  {plan.deferred} more left for a later run by --limit")
    print(f"  left out: {len(plan.invalid)} invalid, {len(plan.stale)} stale")
    if estimate > args.max_usd:
        raise ToolError(
            f"about US${estimate:.2f} is over --max-usd {args.max_usd:.2f}; "
            f"take fewer entries with --limit, or raise the cap"
        )
    if args.dry_run:
        return 0
    if not plan.entries:
        raise ToolError(f"nothing in {args.locale}.json is ready for a review")

    try:
        errors = aws_errors()
    except ImportError as exc:
        raise ToolError(
            f"the AWS SDK is not installed ({exc}); run make setup-venv"
        ) from None
    try:
        client = make_client(args.region, args.profile)
    except errors as exc:
        raise ToolError(f"no Bedrock client could be made: {exc}") from None
    spend, stopped = review_batches(
        client, args.model, rates, prefix, batches, args.max_usd, errors
    )

    counts = counts_of(plan.entries)
    record = {
        "tool": TOOL,
        "locale": args.locale,
        "model": args.model,
        "region": args.region,
        "effort": EFFORT,
        "date": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "glossary": None
        if glossary is None
        else {
            "path": layout.relative(glossary.source),
            "sha256": glossary.sha256,
            "terms": len(glossary.rows),
        },
        "provenance": None
        if provenance is None
        else {
            "path": layout.relative(provenance.source),
            "date": provenance.date,
            "terminologySha256": provenance.terminology_sha256,
            "written": len(provenance.written),
            "notInCatalog": plan.not_in_catalog,
        },
        "translated": plan.translated,
        "reviewed": counts["same"] + counts["same-meaning"] + counts["disagreement"],
        "counts": counts,
        "agreementPercent": agreement_percent(counts),
        "cost": {
            "usd": round(spend.usd, 4),
            "maxUsd": args.max_usd,
            "calls": spend.calls,
            "retriedUsd": round(spend.retried_usd, 4),
            "retriedAttempts": spend.retried,
            **spend.tokens,
        },
        "stopped": stopped,
        "invalid": plan.invalid,
        "invalidProblems": plan.problems,
        "stale": plan.stale,
        "entries": [
            {
                "id": entry.id,
                "english": entry.english,
                "description": entry.description,
                "translation": entry.translation,
                "glossaryTerms": entry.terms,
                "backTranslation": entry.back_translation,
                "outcome": entry.outcome,
                "problems": entry.problems,
                "reason": entry.reason,
                "rendering": entry.rendering,
            }
            for entry in plan.entries
        ],
    }
    write_report(out, record)
    percent = record["agreementPercent"]
    print(
        f"✅ reviewed {record['reviewed']} of {len(plan.entries)}: "
        f"{counts['same']} same, {counts['same-meaning']} same meaning, "
        f"{counts['disagreement']} disagreements"
        f"{'' if percent is None else f' ({percent}% agreement)'}; "
        f"US${spend.usd:.2f} in {spend.calls} call{'' if spend.calls == 1 else 's'}; "
        f"report in {layout.relative(out)}"
    )
    if spend.retried:
        print(f"  {retried_note(spend.retried_usd, spend.retried)}")
    if stopped:
        print(f"  the run stopped early: {stopped}")
    if counts["unreviewed"]:
        print(
            f"  {counts['unreviewed']} entr{'y' if counts['unreviewed'] == 1 else 'ies'} unreviewed (see the report)"
        )
    return 1 if stopped or counts["unreviewed"] else 0


if __name__ == "__main__":
    sys.exit(main())
