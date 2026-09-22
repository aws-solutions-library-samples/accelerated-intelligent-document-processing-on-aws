#!/usr/bin/env python3
"""Shared benchmark utilities: pricing, DDB metering, S3, ground-truth matching.

Resolver-free — reads S3 + DynamoDB directly so it works on any stack version.
All AWS access uses the 'default' profile (the deployment account).
"""

import json
import os
import re
from typing import Any, Literal, NoReturn

import boto3
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRICING_PATH = os.path.join(REPO, "config_library", "pricing.yaml")
REGION = os.environ.get("IDP_REGION", "us-west-2")

_session = None


def session():
    global _session
    if _session is None:
        _session = boto3.Session(
            profile_name=os.environ.get("AWS_PROFILE", "default"), region_name=REGION
        )
    return _session


_clients = {}


def client(name):
    """One cached client per service.

    ``Session.client()`` parses the service model on every call, and the S3 client
    was being rebuilt once per ``read_json`` — tens of thousands of times on a
    release-wide calibration pass over stored runs. Everything here is sequential;
    the cache is a latency fix and nothing more.
    """
    if name not in _clients:
        _clients[name] = session().client(name)
    return _clients[name]


def s3():
    return client("s3")


def ddb():
    return client("dynamodb")


# -------------------------------------------------------------------- readings
class Unread(Exception):
    """A value was demanded from a measurement that was not taken.

    Raised rather than returning a default, because every default this harness
    could pick is a number a reader of a benchmark artifact would take at face
    value.
    """


ReadingState = Literal["present", "absent", "failed"]


class Reading[T]:
    """One measurement, in exactly one of THREE states (GitHub #1079).

    ``present`` — the read succeeded. ``value`` is what was there, and it may
    legitimately be empty or zero: no cost for a cached call, no corrections, no
    missing rows. That is why zero cannot serve as the sentinel for either of the
    other two states.

    ``absent`` — the read succeeded in establishing that there is nothing there:
    the object does not exist, the tracking row was never written. There is no
    measurement, and there is also nothing wrong.

    ``failed`` — the read did not happen. The object may or may not exist and its
    contents are unknown; ``error`` says why. The case this class exists for is a
    release stack whose KMS key entered pending deletion, leaving every object in
    its output bucket present, listable and undecryptable — which the previous
    ``None``-for-everything readers turned into a grid that had recorded nothing.

    The rule the harness follows: **it may continue past a failure, but it may not
    record the failure as a value.** So there is no attribute that yields the value
    without first saying which state you are handling:

    * ``value`` raises :class:`Unread` unless the read is ``present``;
    * ``value_or(default)`` substitutes for ``absent`` only, and raises for
      ``failed`` — a caller that wants to carry on past a failure has to say so,
      and say what it will record about it;
    * ``__bool__`` raises, because ``if reading:`` is precisely the two-state test
      that merges ``absent`` with ``failed``. Truth-testing is how every one of the
      six sites in #1079 lost the distinction, so it is an error here rather than a
      convention documented somewhere else.

    A ``Reading`` also has none of the methods of the thing it wraps, so
    ``reading.get(...)`` and ``reading.items()`` fail the type check as well as the
    run. Do not rely on the type checker alone: ``reportArgumentType`` is disabled
    in ``pyrightconfig.json``, so passing a ``Reading`` where a ``dict`` is declared
    is not reported — the ``__bool__`` guard is what catches that, at the first
    ``metering or {}``.
    """

    __slots__ = ("_error", "_state", "_value")

    def __init__(
        self, state: ReadingState, value: T | None = None, error: str | None = None
    ) -> None:
        self._state: ReadingState = state
        self._value: T | None = value
        self._error: str | None = error

    @classmethod
    def present(cls, value: T) -> "Reading[T]":
        return cls("present", value)

    @classmethod
    def absent(cls, why: str = "") -> "Reading[T]":
        """Nothing there — established, not assumed. ``why`` is for the log only."""
        return cls("absent", None, why or None)

    @classmethod
    def failed(cls, error: object) -> "Reading[T]":
        return cls("failed", None, str(error) or type(error).__name__)

    @property
    def state(self) -> ReadingState:
        return self._state

    @property
    def is_present(self) -> bool:
        return self._state == "present"

    @property
    def is_absent(self) -> bool:
        return self._state == "absent"

    @property
    def is_failed(self) -> bool:
        return self._state == "failed"

    @property
    def error(self) -> str | None:
        """Why the read failed, or the note attached to an absence."""
        return self._error

    @property
    def value(self) -> T:
        if self._state != "present":
            raise Unread(f"no value: this read is {self._state} ({self._error})")
        return self._value  # pyright: ignore[reportReturnType]

    def value_or[D](self, default: D) -> T | D:
        """The value, or ``default`` when the thing read is genuinely ABSENT.

        Raises :class:`Unread` for a failed read. A failure has no value to
        substitute for, and the substitution is the whole defect: it is what turns
        an undecryptable object into "this document recorded nothing".
        """
        if self._state == "failed":
            raise Unread(
                f"read failed ({self._error}) — handle is_failed explicitly and "
                "record the failure; do not substitute a value for it"
            )
        return self._value if self._state == "present" else default  # pyright: ignore[reportReturnType]

    def __bool__(self) -> NoReturn:
        raise TypeError(
            f"a Reading has three states ({self._state} here) — truth-testing one "
            "reads a failed measurement as an absent one. Branch on .is_present / "
            ".is_absent / .is_failed, or call .value_or(default)."
        )

    def __repr__(self) -> str:
        if self._state == "present":
            return f"Reading.present({self._value!r})"
        return f"Reading.{self._state}({self._error!r})"


class SectionRead:
    """Every ``result.json`` under one document prefix, and what could not be read.

    ``sections`` is only the objects that parsed. ``unreadable`` counts the ones
    that were LISTED and then would not read — which is a different fact from a
    document having no sections, and the reason a scorer must not compute an
    accuracy from ``sections`` alone when it is non-zero: an accuracy over the
    subset that happened to decrypt is a wrong number, biased in whichever
    direction the missing sections would have moved it.

    ``listing_error`` is set when the LIST itself failed, in which case it is not
    known whether there are sections at all, and ``sections`` raises
    :class:`Unread` rather than presenting an empty list as an answer.
    """

    __slots__ = ("_sections", "errors", "listing_error", "unreadable")

    def __init__(
        self,
        sections: list[Any],
        unreadable: int = 0,
        errors: tuple[str, ...] = (),
        listing_error: str | None = None,
    ) -> None:
        self._sections = sections
        self.unreadable = unreadable
        self.errors = errors
        self.listing_error = listing_error

    @property
    def sections(self) -> list[Any]:
        if self.listing_error:
            raise Unread(f"section listing failed: {self.listing_error}")
        return self._sections

    @property
    def complete(self) -> bool:
        """True when every object under the prefix was read — the only state in
        which ``sections`` is the whole document."""
        return not self.unreadable and not self.listing_error

    @property
    def why(self) -> str:
        """One line naming what went unread, for an artifact or a console note."""
        if self.listing_error:
            return f"cannot list: {self.listing_error}"
        if not self.unreadable:
            return ""
        return f"{self.unreadable} section object(s) unreadable: " + "; ".join(
            self.errors
        )

    def __bool__(self) -> NoReturn:
        raise TypeError(
            "a SectionRead distinguishes 'no sections' from 'sections that would "
            "not read' — test .complete and .sections, not the object itself."
        )

    def __repr__(self) -> str:
        return (
            f"SectionRead(sections={len(self._sections)}, "
            f"unreadable={self.unreadable}, listing_error={self.listing_error!r})"
        )


# ----------------------------------------------------------------------------- pricing
def load_pricing():
    raw = yaml.safe_load(open(PRICING_PATH))
    table = {}
    for entry in raw["pricing"]:
        units = {}
        for u in entry.get("units") or []:
            try:
                units[u["name"]] = float(u["price"])
            except (TypeError, ValueError):
                pass
        table[entry["name"]] = units
    return table


PRICING = load_pricing()


def price_metering(metering):
    """metering: {'Phase/service/api': {unit: count}}. Price by LONGEST pricing-key
    suffix of the metering key. Returns (total, {matched_key: cost}).

    Both the model key and the unit name are matched EXACTLY — never by substring.
    This is the reference form of the rule; production
    (idp_common/reporting/save_reporting_data.py::_get_unit_cost) implements the
    same one, and did NOT until GitHub issue #926: its substring fallback bound
    'cacheReadInputTokens' to a row's 'inputTokens' price and overcharged cache
    reads by up to 10x. Keep the two in step — a benchmark cost that disagrees
    with the reported cost for the same metering map is a bug in one of them.

    Takes a metering MAP, never a :class:`Reading`. A caller holding a reading has
    to establish that it is ``present`` first — pricing an unread metering row is
    how a failed measurement becomes $0.00 in a published artifact. Passing one
    raises from ``Reading.__bool__`` below rather than pricing it, because
    ``reportArgumentType`` is disabled here and the type checker will not say so.

    ⚠️ **This function still drops what it cannot price, and says nothing about it.**
    An entry whose model has no ``pricing.yaml`` key, or whose unit that key does not
    price, is skipped — so a model added to a run but not to the pricing table makes
    every affected row price **below truth while still reporting a plausible non-zero
    total**, because the other phases price normally. That is the same
    absence-versus-failure defect as
    [#1079](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1079)
    one layer along, on a successful read rather than a failed one, and it is tracked
    as [#1146](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1146).
    Note what does **not** detect it: the absence of a zero cost. The row's cost is
    non-zero and nothing about it reads as partial. All 14 pricing keys the committed
    artifacts use are present with their full unit sets, so no published figure is
    affected today — but check that before adding a model to a suite.
    """
    total = 0.0
    by = {}
    for meter_key, units in (metering or {}).items():
        if not isinstance(units, dict):
            continue
        parts = meter_key.split("/")
        pu = matched = None
        for start in range(len(parts)):
            cand = "/".join(parts[start:])
            if cand in PRICING:
                pu, matched = PRICING[cand], cand
                break
        if not pu:
            continue
        for unit, count in units.items():
            if unit in pu and isinstance(count, (int, float)):
                c = count * pu[unit]
                total += c
                by[matched] = by.get(matched, 0.0) + c
    return total, by


# ----------------------------------------------------------------------------- DDB
def ddb_to_py(v):
    if "M" in v:
        return {k: ddb_to_py(x) for k, x in v["M"].items()}
    if "N" in v:
        return float(v["N"])
    if "S" in v:
        return v["S"]
    if "L" in v:
        return [ddb_to_py(x) for x in v["L"]]
    if "BOOL" in v:
        return v["BOOL"]
    return None


def read_metering(tracking, run_id, doc_name) -> Reading[dict]:
    """Metering map from the ``doc#`` tracking row. Handles Map or JSON-string.

    All three states are reachable and they price differently (GitHub #1079):

    * **present** — the row is there. The map may be ``{}``, which is a real zero:
      a run that consumed no metered unit costs $0.00 and that is a measurement.
    * **absent** — there is no tracking row for this document. Nothing was recorded,
      so there is no cost to report; pricing it as $0.00 would put a number in the
      artifact that was never measured.
    * **failed** — the table could not be read, or ``Metering`` would not decode.
      Returning ``{}`` here is what made a deleted tracking table, a throttled
      request and a genuinely unmetered run all report $0.00, and it is why
      ``aggregate.augment_summary`` is a targeted backfill rather than a re-score.

    The caller decides what to do; ``analyze.score_doc`` refuses to price anything
    but ``present`` and records ``cost_unread`` instead of a zero.
    """
    pk = f"doc#{run_id}/{doc_name}"
    try:
        r = ddb().get_item(
            TableName=tracking,
            Key={"PK": {"S": pk}, "SK": {"S": "none"}},
            ProjectionExpression="Metering",
        )
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
        return Reading.failed(f"{type(exc).__name__}: {exc}")
    item = r.get("Item")
    if not item:
        return Reading.absent(f"no tracking row {pk}")
    if "Metering" not in item:
        # The row exists and carries no metering: nothing was metered for this
        # document. A real zero, and the one state that legitimately prices to $0.
        return Reading.present({})
    m = ddb_to_py(item["Metering"])
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError as exc:
            return Reading.failed(f"Metering is not JSON: {exc}")
    if not isinstance(m, dict):
        return Reading.failed(f"Metering decoded to {type(m).__name__}, not a map")
    return Reading.present(m)


def doc_row(
    tracking,
    run_id,
    doc_name,
    attrs="ObjectStatus,EvaluationStatus,WorkflowStartTime,CompletionTime,PageCount,WorkflowStatus",
):
    pk = f"doc#{run_id}/{doc_name}"
    r = ddb().get_item(
        TableName=tracking,
        Key={"PK": {"S": pk}, "SK": {"S": "none"}},
        ProjectionExpression=attrs,
    )
    it = r.get("Item", {})
    return {k: ddb_to_py(v) for k, v in it.items()}


def _empty_poll():
    return {"total": 0, "obj_done": 0, "eval_done": 0, "failed": 0, "statuses": {}}


def poll_runs(tracking, run_ids):
    """Per-doc status counts for MANY runs in ONE table pass. {run_id: dict}.

    A document's key is ``doc#<run_id>/<doc_name>``, and ``PK`` is the partition
    key — so ``begins_with`` is unavailable (DynamoDB allows it on the sort key
    only) and resolving a run means scanning. That is survivable once per poll
    iteration and is not survivable once per RUN per iteration, which is what
    calling ``poll_run`` in a loop did: a 171-run suite against a stack whose
    tracking table holds months of documents issued 171 full scans per cycle and
    spent longer inside one drain iteration than the runs themselves took (#1016).
    The same call on the launch path is why throughput sat at 12-20 concurrent
    executions against a stack cap of 100 — the harness was scanning, not
    launching. Cost per iteration now scales with the table, not with the table
    times the run count.

    Runs are matched by prefix in memory rather than by a ``contains()`` filter:
    a filter is applied server-side AFTER the read, so it saves transfer but not
    the scan, and one filter cannot select several run ids at once.
    """
    wanted = [r for r in run_ids if r]
    out = {r: _empty_poll() for r in wanted}
    if not wanted:
        return out
    prefixes = [(f"doc#{r}/", r) for r in wanted]

    kw = {
        "TableName": tracking,
        "ProjectionExpression": "PK, ObjectStatus, EvaluationStatus",
    }
    while True:
        r = ddb().scan(**kw)
        for it in r.get("Items", []):
            pk = it.get("PK", {}).get("S", "")
            if not pk.startswith("doc#"):
                continue
            for prefix, rid in prefixes:
                if pk.startswith(prefix):
                    acc = out[rid]
                    acc["total"] += 1
                    o = it.get("ObjectStatus", {}).get("S", "")
                    acc["statuses"][o] = acc["statuses"].get(o, 0) + 1
                    if o == "COMPLETED":
                        acc["obj_done"] += 1
                    if o in ("FAILED", "ERROR"):
                        acc["failed"] += 1
                    if it.get("EvaluationStatus", {}).get("S", "") == "COMPLETED":
                        acc["eval_done"] += 1
                    break
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return out


def poll_run(tracking, run_id):
    """Per-doc status counts for one run. Prefer :func:`poll_runs` in a loop over
    several runs — see the cost note there."""
    return poll_runs(tracking, [run_id])[run_id]


# ----------------------------------------------------------------------------- S3
def list_doc_prefixes(bucket, run_id):
    docs = []
    for p in (
        s3()
        .get_paginator("list_objects_v2")
        .paginate(Bucket=bucket, Prefix=f"{run_id}/", Delimiter="/")
    ):
        for cp in p.get("CommonPrefixes", []):
            docs.append(cp["Prefix"])
    return docs


def _s3_object_is_absent(exc) -> bool:
    """Does this ``get_object`` exception mean the object is not there?

    Only a 404 does. ``AccessDenied``, a KMS key in pending deletion, a throttle and
    a connection reset all mean the object's contents are unknown — and a missing
    BUCKET is a failure too, not an absence: it says nothing about whether the
    objects existed, and reading it as "this grid has no sections" is the shape that
    produced #1079.
    """
    resp = getattr(exc, "response", None)
    if not isinstance(resp, dict):
        return False
    code = str((resp.get("Error") or {}).get("Code") or "")
    if code in ("NoSuchBucket", "NoSuchVersion"):
        # Answered with a 404 status, and not an absent object: the bucket's contents
        # are unknown, so reading this as "the document has no sections" is exactly
        # the substitution #1079 is about.
        return False
    status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in ("NoSuchKey", "404", "NotFound") or status == 404


MAX_READ_ERRORS_REPORTED = 3


def read_json(bucket, key) -> Reading[Any]:
    """One JSON object from S3, as a three-state :class:`Reading` (GitHub #1079).

    This is the root of the absence-versus-failure class: every reader in the
    harness goes through it, so a single ``None`` for "not there" and "could not be
    read" propagated the ambiguity into every metric derived from S3. A 404 is an
    absence; everything else — including a body that is not JSON, which means the
    object exists and is not readable as a result — is a failure.
    """
    try:
        body = s3().get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001 - classified, not swallowed
        if _s3_object_is_absent(exc):
            return Reading.absent(f"s3://{bucket}/{key} does not exist")
        return Reading.failed(f"{key}: {type(exc).__name__}: {exc}")
    try:
        return Reading.present(json.loads(body))
    except ValueError as exc:
        return Reading.failed(f"{key}: not JSON: {exc}")


def read_sections(bucket, doc_prefix) -> SectionRead:
    """Every section ``result.json`` under one document prefix, plus what went unread.

    Replaces a generator that dropped any object ``get_json`` could not parse, so a
    section that failed to decrypt and a section that does not exist produced
    identical output and a scorer downstream computed an accuracy over whichever
    subset happened to read. The count of unreadable objects now comes back with the
    sections, and ``SectionRead.complete`` is the question a scorer has to answer
    before it reports a number.

    A failure to LIST is carried on ``listing_error`` rather than raised, so a
    130-run scoring pass survives one dead prefix — but ``SectionRead.sections``
    raises for it, so surviving it still requires handling it.
    """
    sections: list[Any] = []
    unreadable = 0
    errors: list[str] = []
    try:
        pages = list(
            s3()
            .get_paginator("list_objects_v2")
            .paginate(Bucket=bucket, Prefix=doc_prefix + "sections/")
        )
    except Exception as exc:  # noqa: BLE001 - reported on the result object
        return SectionRead(
            [], listing_error=f"{doc_prefix}sections/: {type(exc).__name__}: {exc}"
        )
    for pg in pages:
        for o in pg.get("Contents", []):
            if not o["Key"].endswith("result.json"):
                continue
            read = read_json(bucket, o["Key"])
            if read.is_present:
                sec = read.value
                if isinstance(sec, dict):
                    sections.append(sec)
                    continue
                # Parsed, but not a section object. The old walk dropped anything
                # falsy, which silently included this; it is malformed output, so it
                # counts as unreadable rather than as a section that is not there.
                read = Reading.failed(
                    f"{o['Key']}: parsed as {type(sec).__name__}, not a section object"
                )
            # An object that was LISTED and then would not read is unreadable, not
            # absent — the two are only the same if you ignore the listing. A 404
            # here means it was deleted between the list and the get, which is still
            # not a section this document does not have.
            unreadable += 1
            if len(errors) < MAX_READ_ERRORS_REPORTED:
                errors.append(str(read.error))
    return SectionRead(sections, unreadable, tuple(errors))


# ----------------------------------------------------------------------------- GT matching
SEQ = re.compile(r"SEQ(\d{5})")


def walk_confidence(explainability_info):
    """``{field path: confidence}`` for one section's ``explainability_info``.

    This used to append the bare scalar to a list and throw the path away, which
    put a ceiling on everything the harness could say about confidence: a score
    with no path cannot be joined to the cell it describes, so the only available
    statistics were distributional (``mean_confidence``, ``pct_conf_below_0.9``)
    and calibration against ground truth was unmeasurable on the one corpus that
    has exact per-cell truth (GitHub #935).

    The traversal is NOT implemented here. ``flatten_confidences`` is the rule the
    product itself keys stored confidence curves by, and its sibling
    ``flatten_values`` keys an ``inference_result`` identically — so a path from
    one indexes straight into the other, which is the entire join. A private copy
    of the walk in the harness would drift from the one the shipped curve uses,
    and then a harness calibration number and a stored curve would disagree for
    reasons that have nothing to do with the data.

    Requires ``idp_common`` on ``PYTHONPATH`` (pinned in
    benchmarks/matrices/METHODOLOGY.md). ``confidence_curve`` and ``curve_store``
    are deliberately standard-library-only, so this import does not pull Stickler.
    """
    from idp_common.evaluation import flatten_confidences

    return flatten_confidences(explainability_info)


def confidence_values(explainability_info):
    """Just the confidence scores, for the distribution-only statistics.

    Paths are unique by construction, so this is the same multiset the old
    list-appending walk produced and ``mean_confidence`` / ``pct_conf_below_0.9``
    / ``n_conf_leaves`` are unchanged — verified against stored v0.6.9 sections
    across scalar, table, multi-section and multi-instance shapes. The committed
    baselines stay comparable.
    """
    return list(walk_confidence(explainability_info).values())


def find_list(node, key_lc=("transactions",)):
    """Return the first list value whose key matches (case-insensitive)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() in key_lc and isinstance(v, list):
                return v
            r = find_list(v, key_lc)
            if r is not None:
                return r
    elif isinstance(node, list):
        for i in node:
            r = find_list(i, key_lc)
            if r is not None:
                return r
    return None
