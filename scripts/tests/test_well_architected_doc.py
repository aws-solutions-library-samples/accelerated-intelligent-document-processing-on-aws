# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Keep the numbers on ``docs/well-architected.md`` true.

That page is a customer-facing review template. Its value is that every factual
claim names a template, resource, parameter or count the reader can go and verify,
which means a wrong number there does not merely read badly — it gives someone the
wrong answer about their own security or recoverability posture and they act on it.

Nothing checked any of those claims until this file existed, and four of them were
wrong at once: an SNS email subscription that no template creates, every
dead-letter-queue number attributed to the wrong queue, "authorization is enforced
per operation on Cognito group membership" when 26 of 118 operations need only a
valid session, and a point-in-time-recovery figure that contradicted the same page
two sections earlier. ``scripts/tests/test_testing_doc.py``, which keeps
``docs/testing.md`` from drifting away from the Makefile, is the precedent this
file follows.

Four groups of assertions:

1. every ``make`` target the page cites resolves in the Makefile it is invoked from;
2. every logical id, parameter name and repository path the page cites exists;
3. every count the page states matches a fresh count of the templates and of
   ``scripts/api_rbac_expectations.yaml``;
4. every external link's *final* URL equals its requested URL.

**Counts are derived, never enumerated.** Each check reads the number out of the
templates at test time and formats it into the sentence the page is expected to
contain. There is no table of expected values in this file to go stale, because a
hardcoded inventory that nobody re-derives is the very defect class this guard
exists to catch.

**Group 4 does not run by default.** ``pytest scripts/tests`` runs in both CIs with
no guarantee of egress, and a link checker that fails on a network error red-lines
every pull request for reasons unrelated to the change. It is therefore opt-in via
``CHECK_DOC_LINKS=1``, and even then it skips (rather than fails) if a control URL
is unreachable, so an opt-in run behind a proxy reports "could not check" instead of
"broken". It is not a no-op: with the variable set and egress available it fetches
every link and fails on any redirect or non-2xx status.

Why the final URL and not the status code: ``docs.aws.amazon.com`` answers **200**
for a retired page by redirecting to the enclosing guide's index. A status-code link
checker passes a link that no longer points at the content it claims to. Comparing
``curl --location``'s effective URL against the requested one is what catches that.
The reverse trap is just as easy — a URL that 404s does not redirect, so the
effective URL matches and a redirect-only check passes it. Both are asserted.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC = REPO_ROOT / "docs" / "well-architected.md"
MAKEFILE = REPO_ROOT / "Makefile"
PARENT_TEMPLATE = REPO_ROOT / "template.yaml"
UNIFIED_TEMPLATE = REPO_ROOT / "patterns" / "unified" / "template.yaml"
API_TEMPLATE = REPO_ROOT / "nested" / "api-resolvers" / "template.yaml"
RBAC_EXPECTATIONS = REPO_ROOT / "scripts" / "api_rbac_expectations.yaml"
SIDEBAR = REPO_ROOT / "docs-site" / "astro.config.mjs"

# Trees that are not part of the deployed solution or its optional extensions:
# the SDLC pipeline templates, the notebook and lambda-hook samples, the workshop.
# The page's counts are about what a customer deploys, so these are out of scope —
# and saying so here is what makes the scope reproducible rather than a judgement
# call re-made on every edit.
NON_SOLUTION_TREES = ("scripts", "notebooks", "samples", "workshop")

TARGET_RE = re.compile(r"^([a-zA-Z0-9_.-]+):", re.MULTILINE)
# `make foo`, `make -C some/dir foo`
MAKE_CALL_RE = re.compile(r"`make (?:-C\s+(\S+)\s+)?([a-z][a-z0-9-]*)")
BACKTICK_IDENT_RE = re.compile(r"`([A-Z][A-Za-z0-9]+)`")
BACKTICK_PATH_RE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./+-]*/[A-Za-z0-9_./+-]*)`")
EXTERNAL_LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)")
RELATIVE_LINK_RE = re.compile(r"\]\((\.[^)\s#]+)(?:#[^)\s]*)?\)")

NUMBER_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
    20: "twenty",
    21: "twenty-one",
    22: "twenty-two",
    23: "twenty-three",
    24: "twenty-four",
}

# CloudFormation property names, metric names and enum values the page quotes in
# backticks. They are not logical ids, so the identifier check must not demand that
# they resolve to a resource — but each is here with a reason, because an
# unexplained exemption is how a genuinely wrong id gets waved through later.
NOT_LOGICAL_IDS = {
    "BackoffRate": "Step Functions Retry field name",
    "Block": "WAFv2 default-action value",
    "Catch": "Step Functions state field name",
    "ConditionExpression": "DynamoDB UpdateItem parameter name",
    "DEBUG": "LogLevel enum value",
    "DRAFT": "BedrockGuardrailVersion default value",
    "DeadLetterQueue": "SAM function property name",
    "ERROR": "LogLevel enum value",
    "ExecutionTime": "AWS/States metric name",
    "ExecutionsFailed": "AWS/States metric name",
    "ExecutionsTimedOut": "AWS/States metric name",
    "INFO": "LogLevel enum value",
    "MaxAttempts": "Step Functions Retry field name",
    "MfaConfiguration": "property the user pool deliberately does NOT set — the "
    "page's point is its absence, so it cannot be required to appear",
    "OnFailure": "Lambda EventInvokeConfig destination key",
    "PublicAccessBlockConfiguration": "S3 bucket property name",
    "RedrivePolicy": "SQS queue property name",
    "Retry": "Step Functions state field name",
    "TracingConfiguration": "state-machine property the template deliberately does "
    "NOT set — the page's point is its absence",
    "VisibilityTimeout": "SQS queue property name",
    "WARN": "LogLevel enum value",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse newlines so a claim that wraps mid-sentence still matches."""
    return re.sub(r"\s+", " ", text)


def _targets(makefile: Path) -> set[str]:
    return set(TARGET_RE.findall(makefile.read_text(encoding="utf-8")))


def _solution_templates() -> list[tuple[Path, str]]:
    """Every CloudFormation template a customer deploys, parent plus extensions."""
    found = []
    for path in sorted(REPO_ROOT.rglob("*.y*ml")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in NON_SOLUTION_TREES:
            continue
        if any(
            p in {"node_modules", ".aws-sam", ".venv", "build", "dist"}
            for p in rel.parts
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "AWSTemplateFormatVersion" in text:
            found.append((rel, text))
    return found


def _resource_blocks(template: Path) -> dict[str, str]:
    """Map top-level logical id -> raw block text, by indentation.

    Deliberately textual: these templates use CloudFormation short-form tags
    (``!Ref``, ``!GetAtt``) that ``yaml.safe_load`` rejects, and the counts here are
    about what the source declares, not about a resolved graph.
    """
    lines = template.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if re.match(r"^  [A-Za-z0-9]+:\s*$", ln)]
    blocks = {}
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        name = re.match(r"^  ([A-Za-z0-9]+):", lines[i]).group(1)
        blocks[name] = "\n".join(lines[i:end])
    return blocks


def _of_type(template: Path, resource_type: str) -> dict[str, str]:
    return {
        name: body
        for name, body in _resource_blocks(template).items()
        if re.search(rf"Type:\s*{re.escape(resource_type)}\s*$", body, re.MULTILINE)
    }


def _assert_phrase(phrase: str, why: str) -> None:
    """Assert a derived sentence fragment appears on the page (case-insensitive)."""
    if phrase.lower() not in _flat(_doc()).lower():
        pytest.fail(
            f"docs/well-architected.md no longer says {phrase!r}.\n"
            f"{why}\n"
            "The number above was counted from the templates just now, so either the "
            "page is stale or the wording drifted away from what this guard checks."
        )


_ANY_NUMBER = (
    "(" + "|".join([r"\d+", *(w for _, w in sorted(NUMBER_WORDS.items()))]) + ")"
)
_WORD_TO_INT = {w: n for n, w in NUMBER_WORDS.items()}


def _assert_count_phrase(
    n: int, template_str: str, why: str, *, strict: bool = True
) -> None:
    """Assert the page states ``n`` in the given phrase, in digits or as a word.

    With ``strict`` (the default) *every* occurrence of the phrase on the page has
    to carry the same number, not merely one of them. The page previously said "22
    TLS-only resource policies" in the summary table and "twenty-three" in the
    Security pillar — a contradiction that a "does the right number appear
    somewhere" check passes. Pass ``strict=False``, with a reason, only where the
    phrase is generic enough that a different number legitimately precedes it
    elsewhere.
    """
    flat = _flat(_doc())
    forms = [template_str.format(n=str(n))]
    if n in NUMBER_WORDS:
        forms.append(template_str.format(n=NUMBER_WORDS[n]))
    if not any(f.lower() in flat.lower() for f in forms):
        pytest.fail(
            "docs/well-architected.md does not state the measured value "
            f"{n} where it should.\nExpected one of: "
            + " | ".join(repr(f) for f in forms)
            + f"\n{why}"
        )
    if not strict:
        return
    # Match the whole phrase, keeping any text before the number, and refuse to
    # start mid-word: without the lookbehind, "S3 buckets" matches "3 buckets" and
    # "none of the twelve alarms" matches "one of the twelve alarms".
    before, after = re.escape(template_str.format(n="\x00")).split("\x00", 1)
    pattern = re.compile(before + r"(?<![\w-])" + _ANY_NUMBER + after, re.IGNORECASE)
    disagreeing = sorted(
        {
            m.group(1)
            for m in pattern.finditer(flat)
            if _WORD_TO_INT.get(m.group(1).lower(), _as_int(m.group(1))) != n
        }
    )
    assert not disagreeing, (
        f"docs/well-architected.md states more than one value for {template_str!r}: "
        f"measured {n}, but the page also says {disagreeing}.\n{why}\n"
        "Two different numbers for the same thing on one page is the defect this "
        "check exists to catch — fix the stale one rather than relaxing the check."
    )


def _as_int(token: str) -> int | None:
    return int(token) if token.isdigit() else None


# --------------------------------------------------------------------------- #
# group 0 — the page itself
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_the_page_exists_with_frontmatter_and_licence() -> None:
    text = _doc()
    assert text.startswith(
        '---\ntitle: "AWS Well-Architected Framework Review"\n---'
    ), "docs/*.md needs YAML frontmatter with a title; the docs site keys off it"
    assert "SPDX-License-Identifier: MIT-0" in text.split("# AWS Well-Architected")[0]


@pytest.mark.unit
def test_the_page_is_reachable_from_the_sidebar() -> None:
    """The slug is what docs-site/setup.sh symlinks; changing it unpublishes the page."""
    assert 'slug: "well-architected"' in SIDEBAR.read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_checklist_ships_empty() -> None:
    """Every checklist row must keep its three customer columns blank.

    A pre-filled row is worse than a missing one: the customer reads an answer that
    is not theirs and moves on. Rows must also be questions, not assertions.
    """
    rows = [
        ln
        for ln in _doc().splitlines()
        if ln.startswith("| ") and ln.rstrip().endswith("| | | |")
    ]
    assert len(rows) >= 50, f"expected the full checklist, found {len(rows)} rows"
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        assert len(cells) == 4, f"row is not four columns: {row}"
        assert cells[1] == cells[2] == cells[3] == "", f"row ships pre-filled: {row}"
        assert "?" in cells[0], f"checklist row is not phrased as a question: {row}"


# --------------------------------------------------------------------------- #
# group 1 — make targets
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_every_make_target_the_page_cites_exists() -> None:
    """A cited target that does not resolve sends the reader to an error.

    ``make test-integration`` from the repo root exited 2 with "No rule to make
    target" while the page told customers to run it: the target lives in
    ``lib/idp_common_pkg/Makefile``, not the root one.
    """
    root_targets = _targets(MAKEFILE)
    broken = []
    for directory, target in MAKE_CALL_RE.findall(_doc()):
        if directory:
            makefile = REPO_ROOT / directory / "Makefile"
            if not makefile.is_file():
                broken.append(f"make -C {directory} {target}  (no Makefile there)")
            elif target not in _targets(makefile):
                broken.append(f"make -C {directory} {target}  (not a target there)")
        elif target not in root_targets:
            broken.append(f"make {target}  (not a target in the root Makefile)")
    assert not broken, (
        "docs/well-architected.md cites make targets that do not resolve:\n"
        + "\n".join(f"  - {b}" for b in broken)
    )


# --------------------------------------------------------------------------- #
# group 2 — identifiers and paths
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_every_identifier_the_page_cites_exists_in_a_template() -> None:
    """Logical ids, parameter names and condition names must be real.

    The page's whole method is "go and look at this in your own stack", so an id
    that does not exist is the one error that cannot be shrugged off.
    """
    universe: set[str] = set()
    for _, text in _solution_templates():
        universe.update(re.findall(r"^  ([A-Za-z0-9]+):", text, re.MULTILINE))
        universe.update(re.findall(r"\b([A-Z][A-Za-z0-9]+)\b", text))
    unknown = sorted(
        ident
        for ident in set(BACKTICK_IDENT_RE.findall(_doc()))
        if ident not in universe and ident not in NOT_LOGICAL_IDS
    )
    assert not unknown, (
        "docs/well-architected.md cites identifiers that appear in no solution "
        f"template: {unknown}\nEither the name is wrong, or it is a property/metric "
        "name that belongs in NOT_LOGICAL_IDS with a reason."
    )


@pytest.mark.unit
def test_exemptions_are_still_needed() -> None:
    """An exemption that is no longer cited is dead weight that hides the next one."""
    cited = set(BACKTICK_IDENT_RE.findall(_doc()))
    stale = sorted(name for name in NOT_LOGICAL_IDS if name not in cited)
    assert not stale, f"NOT_LOGICAL_IDS entries the page no longer cites: {stale}"


@pytest.mark.unit
def test_every_repository_path_the_page_cites_exists() -> None:
    missing = []
    for candidate in sorted(set(BACKTICK_PATH_RE.findall(_doc()))):
        if re.match(r"^\d+\.\d+\.\d+\.\d+/\d+$", candidate):  # 0.0.0.0/0
            continue
        if not (REPO_ROOT / candidate).exists():
            missing.append(candidate)
    assert not missing, (
        f"docs/well-architected.md cites paths that do not exist: {missing}"
    )


@pytest.mark.unit
def test_every_relative_doc_link_resolves() -> None:
    missing = [
        link
        for link in sorted(set(RELATIVE_LINK_RE.findall(_doc())))
        if not (DOC.parent / link).resolve().exists()
    ]
    assert not missing, f"docs/well-architected.md links to missing docs: {missing}"


@pytest.mark.unit
def test_the_api_route_path_parameter_matches_the_template() -> None:
    """The page named ``POST /op/{operation}``; the template declares ``{field}``.

    A route a customer cannot find in the template is indistinguishable, to them,
    from a route that is not authorized the way the page says it is.
    """
    parts = re.findall(
        r'PathPart:\s*"?([^"\s]+)"?', API_TEMPLATE.read_text(encoding="utf-8")
    )
    assert "op" in parts, (
        "the api-resolvers template no longer declares an /op resource"
    )
    proxied = [p for p in parts if p.startswith("{") and p != "{proxy+}"]
    assert proxied, "no path-parameter PathPart found in the api-resolvers template"
    for param in proxied:
        _assert_phrase(
            f"/op/{param}",
            f"The template declares PathPart {param!r}, so that is the route the page "
            "must name.",
        )


@pytest.mark.unit
def test_every_api_gateway_method_is_named_on_the_page() -> None:
    """Calling ``POST /op/{field}`` "the single route" hid three others.

    Two of them, the Web UI static-asset GETs, are ``AuthorizationType: NONE``. They
    are deliberate and documented in the template, but a page whose subject is
    authorization has to say they exist.
    """
    methods = _of_type(API_TEMPLATE, "AWS::ApiGateway::Method")
    assert len(methods) >= 4, f"expected the four REST methods, found {sorted(methods)}"
    missing = sorted(name for name in methods if f"`{name}`" not in _doc())
    assert not missing, (
        "docs/well-architected.md does not mention these AWS::ApiGateway::Method "
        f"resources: {missing}. The page must not describe one route as the only one."
    )


# --------------------------------------------------------------------------- #
# group 3 — derived counts
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_s3_bucket_count() -> None:
    n = len(_of_type(PARENT_TEMPLATE, "AWS::S3::Bucket"))
    _assert_count_phrase(
        n, "{n} buckets", f"template.yaml declares {n} AWS::S3::Bucket resources."
    )


@pytest.mark.unit
def test_tls_only_resource_policy_count() -> None:
    """The page said 22 in one place and twenty-three in another. It is 23."""
    n = sum(
        text.count("aws:SecureTransport")
        for rel, text in _solution_templates()
        if rel.parts[0] not in NON_SOLUTION_TREES
    )
    _assert_count_phrase(
        n,
        "{n} TLS-only",
        f"{n} statements across the solution templates deny aws:SecureTransport=false.",
    )
    _assert_count_phrase(
        n,
        "{n} resource policies deny requests",
        "The Security pillar's in-transit paragraph must state the same total.",
    )


@pytest.mark.unit
def test_dynamodb_table_and_pitr_counts() -> None:
    """A DR reviewer reads the DR section, which is where the wrong figure was."""
    default_deployment = [PARENT_TEMPLATE, UNIFIED_TEMPLATE, API_TEMPLATE]
    tables, with_pitr = 0, 0
    for template in default_deployment:
        for _, body in _of_type(template, "AWS::DynamoDB::Table").items():
            tables += 1
            if "PointInTimeRecoveryEnabled: true" in body:
                with_pitr += 1
    all_tables = sum(
        text.count("AWS::DynamoDB::Table")
        for rel, text in _solution_templates()
        if rel.parts[0] not in NON_SOLUTION_TREES
    )
    _assert_count_phrase(
        tables,
        "{n} DynamoDB tables a default deployment creates",
        f"A default deployment creates {tables} tables.",
    )
    _assert_count_phrase(
        with_pitr,
        f"{{n}} of the {NUMBER_WORDS[tables]} DynamoDB tables",
        f"{with_pitr} of those {tables} tables set PointInTimeRecoveryEnabled: true. "
        "The Reliability pillar and the Disaster Recovery section must agree.",
    )
    _assert_count_phrase(
        all_tables,
        "{n} tables",
        f"Counting the optional feature-platform extensions there are {all_tables}.",
    )
    # Both exceptions must be named where a DR reviewer will read them.
    without = [
        name
        for template in default_deployment
        for name, body in _of_type(template, "AWS::DynamoDB::Table").items()
        if "PointInTimeRecoveryEnabled: true" not in body
    ]
    dr_section = _doc().split("### Disaster Recovery", 1)[-1]
    for name in without:
        assert f"`{name}`" in dr_section, (
            f"{name} has no point-in-time recovery but is not named in the Disaster "
            "Recovery section, which is the section a DR reviewer reads."
        )


@pytest.mark.unit
def test_cloudwatch_alarm_counts() -> None:
    alarms = _of_type(PARENT_TEMPLATE, "AWS::CloudWatch::Alarm")
    to_alerts = [n for n, body in alarms.items() if "!Ref AlertsTopic" in body]
    _assert_count_phrase(
        len(alarms),
        "{n} `AWS::CloudWatch::Alarm` resources",
        f"template.yaml declares {len(alarms)} alarms.",
    )
    _assert_count_phrase(
        len(to_alerts),
        # "publish to" is part of the phrase on purpose: the page also, correctly,
        # says "Four of the twelve alarms watch DLQs", which is a different subset.
        "{n} of the {} alarms publish to".format(NUMBER_WORDS[len(alarms)], n="{n}"),
        f"{len(to_alerts)} of them reference AlertsTopic.",
    )


@pytest.mark.unit
def test_cognito_user_pool_group_count() -> None:
    n = len(_of_type(PARENT_TEMPLATE, "AWS::Cognito::UserPoolGroup"))
    _assert_count_phrase(
        n,
        "{n} `AWS::Cognito::UserPoolGroup` resources",
        f"template.yaml declares {n} user pool groups; a self-registered user is in none.",
    )


@pytest.mark.unit
def test_redrive_policy_table_matches_the_queues() -> None:
    """Every DLQ number on the page was wrong, and attributed to the wrong queue.

    ``maxReceiveCount`` 1000 is ``DiscoveryQueue``'s against a 900s timeout (~250
    hours of retrying), not ``DocumentQueue``'s against 30s (~8 hours). The 500/60s
    pair that really does give ~8 hours belongs to ``DocumentQueue``. The stale
    source was a comment in ``template.yaml`` that the page copied faithfully.
    """
    measured = {}
    for name, body in _of_type(PARENT_TEMPLATE, "AWS::SQS::Queue").items():
        mrc = re.search(r"maxReceiveCount:\s*(\d+)", body)
        if not mrc:
            continue
        vt = re.search(r"VisibilityTimeout:\s*(\d+)", body)
        assert vt, f"{name} sets maxReceiveCount but no VisibilityTimeout"
        measured[name] = (int(vt.group(1)), int(mrc.group(1)))

    flat = _flat(_doc())
    for name, (visibility, max_receive) in sorted(measured.items()):
        row = f"| `{name}` | {visibility}s | {max_receive} |"
        assert row in flat, (
            f"docs/well-architected.md has no DLQ table row {row!r}.\n"
            f"{name} declares VisibilityTimeout {visibility} and maxReceiveCount "
            f"{max_receive}; the page must attribute both to that queue."
        )

    # A queue with no RedrivePolicy must not be swept into "every SQS consumer".
    for name, body in _of_type(PARENT_TEMPLATE, "AWS::SQS::Queue").items():
        if name in measured or "DLQ" in name:
            continue
        assert "Every SQS consumer has a DLQ" not in flat, (
            f"{name} declares no RedrivePolicy, so the page must not claim every SQS "
            "consumer has a DLQ."
        )


@pytest.mark.unit
def test_api_authorization_counts_match_the_expectations_file() -> None:
    """``scripts/api_rbac_expectations.yaml`` is the declared source of truth.

    The page claimed authorization was enforced per operation on group membership.
    It is not: a quarter of the operations are ``groups: ANY``, and most of those
    carry no ownership or configuration-version narrowing either. That is the
    designed posture, so the code is not the defect — the page was.
    """
    spec = yaml.safe_load(RBAC_EXPECTATIONS.read_text(encoding="utf-8"))
    ops = spec["operations"]
    iam_only = sorted(n for n, s in ops.items() if s.get("groups") == "IAM_ONLY")
    any_auth = sorted(n for n, s in ops.items() if s.get("groups") == "ANY")
    group_restricted = len(ops) - len(iam_only) - len(any_auth)
    narrowing = ("ownership", "scope_checked", "scope_filtered")
    unnarrowed = sorted(n for n in any_auth if not any(k in ops[n] for k in narrowing))

    _assert_count_phrase(
        len(ops),
        "{n} operations",
        f"The expectations file covers {len(ops)} operations.",
    )
    _assert_count_phrase(
        group_restricted,
        "{n} of them are restricted to named Cognito groups",
        f"{group_restricted} operations declare an explicit group list.",
    )
    _assert_count_phrase(
        len(iam_only),
        "{n} (`",
        f"{len(iam_only)} operations are IAM_ONLY and the page introduces them by count "
        "followed by a parenthesised list.",
    )
    for name in iam_only:
        assert f"`{name}`" in _doc(), (
            f"{name} is declared IAM_ONLY — reachable only by IAM principals, not by any "
            "Cognito caller — and the page does not name it."
        )
    _assert_count_phrase(
        len(any_auth),
        "remaining {n} are declared `groups: ANY`",
        f"{len(any_auth)} operations are reachable by any authenticated user.",
    )
    _assert_count_phrase(
        len(any_auth) - len(unnarrowed),
        "{n} of those " + str(len(any_auth)) + " are narrowed further",
        "Ownership- or scope-narrowed ANY operations.",
    )
    _assert_count_phrase(
        len(unnarrowed),
        "other {n} are not",
        f"{len(unnarrowed)} ANY operations carry no ownership or scope key at all.",
    )
    # The read surface a customer most needs to see named.
    for name in (
        "getDocument",
        "getFileContents",
        "getFilePresignedUrl",
        "queryKnowledgeBase",
    ):
        if name in unnarrowed:
            assert f"`{name}`" in _doc(), (
                f"{name} is reachable by any authenticated user with no further check "
                "and is not named on the page."
            )


@pytest.mark.unit
def test_wildcard_resource_statement_count() -> None:
    """The page states a wildcard IAM surface; it has to be the real one."""
    inline = re.compile(r'Resource:\s*["\']?\*["\']?\s*$')
    bare_item = re.compile(r'^\s*-\s*["\']?\*["\']?\s*$')
    total, templates_hit = 0, set()
    per_template: dict[str, int] = {}
    for rel, text in _solution_templates():
        lines = text.splitlines()
        count = 0
        for i, line in enumerate(lines):
            if inline.search(line):
                count += 1
            elif bare_item.match(line):
                j = i - 1
                while j >= 0 and not lines[j].strip():
                    j -= 1
                if j >= 0 and re.match(r"^\s*Resource:\s*$", lines[j]):
                    count += 1
        if count:
            per_template[str(rel)] = count
            templates_hit.add(str(rel))
            total += count

    _assert_count_phrase(
        total,
        '{n} IAM policy statements are written against `Resource: "*"`',
        f"{total} wildcard-resource statements across {len(templates_hit)} templates.",
    )
    _assert_count_phrase(
        len(templates_hit),
        "across the {n} templates",
        f"{len(templates_hit)} solution templates contain at least one.",
    )
    for rel in ("template.yaml", "patterns/unified/template.yaml"):
        _assert_count_phrase(
            per_template[rel],
            "{n} in `" + rel + "`",
            f"{rel} contains {per_template[rel]} of them.",
            # "N in `template.yaml`" is a shape the page legitimately uses for
            # several different inventories (alarms, buckets, tables), so requiring
            # every occurrence to carry the wildcard number would be wrong here.
            strict=False,
        )


@pytest.mark.unit
def test_parent_stack_resource_headroom() -> None:
    """The nested-stack split is a headroom budget as well as fault isolation."""
    text = PARENT_TEMPLATE.read_text(encoding="utf-8")
    body = text.split("\nResources:\n", 1)[1]
    body = re.split(r"^[A-Za-z]", body, maxsplit=1, flags=re.MULTILINE)[0]
    n = len(re.findall(r"^  ([A-Za-z0-9]+):\s*$", body, re.MULTILINE))
    _assert_count_phrase(
        n,
        "{n} top-level resources",
        f"template.yaml declares {n} top-level resources against the 500 limit.",
    )
    _assert_phrase("limit of 500", "CloudFormation's per-stack resource limit.")


@pytest.mark.unit
def test_alerts_topic_subscription_claim_matches_the_template() -> None:
    """The page asserted an email subscription that no template creates.

    Eleven alarms publish to ``AlertsTopic``, so on a default deployment those
    eleven notify nobody. This check is conditional on the template rather than on
    a fixed expectation, so it keeps holding if a subscription is later added: what
    it forbids is the page asserting one exists while none does.
    """
    subscriptions = _of_type(PARENT_TEMPLATE, "AWS::SNS::Subscription")
    to_alerts = [n for n, body in subscriptions.items() if "AlertsTopic" in body]
    flat = _flat(_doc())
    if not to_alerts:
        forbidden = [
            "the stack creates an email subscription",
            "the stack subscribes",
            "an email subscription for `AdminEmail`",
        ]
        asserted = [p for p in forbidden if p.lower() in flat.lower()]
        assert not asserted, (
            "No AWS::SNS::Subscription in template.yaml targets AlertsTopic, but "
            f"docs/well-architected.md asserts one exists: {asserted}"
        )
    assert "subscription list" in flat.lower(), (
        "The page must tell the reader to read the topic's actual subscription list "
        "rather than assume, because that is true whether or not the stack creates one."
    )


# --------------------------------------------------------------------------- #
# group 4 — external links (opt-in; see the module docstring)
# --------------------------------------------------------------------------- #
CONTROL_URL = (
    "https://docs.aws.amazon.com/wellarchitected/latest/framework/welcome.html"
)


def _probe(url: str) -> tuple[str, str]:
    """Return (final URL, status) after following redirects. ('', '') on failure."""
    try:
        out = subprocess.run(
            [
                "curl",
                "-sS",
                "-L",
                "-o",
                "/dev/null",
                "-w",
                "%{url_effective} %{http_code}",
                "--max-time",
                "25",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", ""
    if out.returncode != 0:
        return "", ""
    parts = out.stdout.strip().rsplit(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", "")


@pytest.mark.unit
def test_external_links_are_not_redirected_or_dead() -> None:
    if os.environ.get("CHECK_DOC_LINKS") != "1":
        pytest.skip(
            "network check: set CHECK_DOC_LINKS=1 to fetch every external link. Off by "
            "default so no-egress CI runs cannot red-line a pull request."
        )
    if _probe(CONTROL_URL) == ("", ""):
        pytest.skip(
            f"no egress: control URL {CONTROL_URL} unreachable, so nothing was checked"
        )

    urls = sorted(set(EXTERNAL_LINK_RE.findall(_doc())))
    assert urls, "no external links found — the extraction regex has drifted"
    problems = []
    for url in urls:
        final, status = _probe(url)
        if (final, status) == ("", ""):
            problems.append(f"{url}\n      could not be fetched")
            continue
        if not status.startswith("2"):
            problems.append(f"{url}\n      HTTP {status}")
        elif final != url:
            problems.append(f"{url}\n      redirects to {final}")
    assert not problems, (
        "docs/well-architected.md has external links that no longer point at what "
        "they claim.\nA 200 is not enough: docs.aws.amazon.com answers 200 for a "
        "retired page by redirecting to the guide index.\n"
        + "\n".join(f"  - {p}" for p in problems)
    )
