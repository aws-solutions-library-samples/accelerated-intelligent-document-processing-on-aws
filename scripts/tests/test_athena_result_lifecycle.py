# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every Athena output prefix must be covered by an S3 lifecycle rule.

Athena query output is scratch: each `SELECT` leaves a result object and a
`.csv.metadata` beside it, and an `INSERT INTO` leaves a
`<query_id>-manifest.txt` as well. Nothing deletes them. Measured at 147,755
objects on one development stack, from dashboard refreshes alone, with the
bucket-wide `DataRetentionInDays` rule (default 365) the only thing covering
them.

⚠️ **The risk this guards is a spelling difference, and it is the kind review is
bad at.** The reporting bucket holds Athena output under **two** prefixes:
`athena-results/`, where every consumer but one writes via
`ATHENA_OUTPUT_LOCATION`, and `athena_query_results/` — *underscores* — where
`DataMartRollupFunction` writes via `ATHENA_QUERY_OUTPUT_LOCATION`. How many
sites each has is not written down here: this gate enumerates them, so read
`_LOCATIONS` or the parametrised case ids for the current set. A lifecycle rule for one does not cover the
other, and the underscore prefix is the one it would hurt most to leave
uncovered: three EventBridge schedules fire the rollup, so it is the only Athena
output that accrues with nobody logged in.

So the prefix set is **derived from the templates** rather than listed here, and
so is the coverage:

* the prefixes come from every `ATHENA_*OUTPUT_LOCATION` environment variable in
  every template `scripts/discover_templates.sh` finds — by content, so a new
  template is covered without being listed here;
* the coverage comes from every `AWS::S3::Bucket`'s `LifecycleConfiguration` in
  those same templates.

A seventh consumer with an eighth prefix therefore fails here rather than
quietly accruing.

⚠️ **Three bounds, stated so a green run is not over-read.**

**A bucket this template does not create is not checked at all.**
`ReportingBucket` carries `Condition: ShouldCreateReportingBucket`
(`!Equals [!Ref ReportingBucketName, ""]`), so an operator who supplies their
own bucket gets no lifecycle rules from this stack — not these and not the
bucket-wide `DataRetentionInDays` one. This gate reads the rule off the
resource unconditionally, so it reports coverage for a configuration in which
the rule does not exist. That is a deliberate scope decision — the template
cannot manage a bucket it does not own — but it means a green run here says
"covered when this stack creates the bucket", not "covered".

**A location whose bucket is a parameter or an `ImportValue`** — which is how
`nested/api-resolvers/template.yaml` receives the reporting bucket — cannot be
resolved to a bucket resource from inside its own template, so for those the
assertion is only that *some* template declares a rule covering the prefix, not
that it is the right bucket. Where the bucket does resolve to a resource in the
same template, that specific bucket is checked.

**This reasons about the template, not about runtime.** A consumer that builds
its output location in Python from a bucket name, rather than taking it from an
`ATHENA_*OUTPUT_LOCATION` variable, is invisible here. The naming convention is
what makes the derivation work, so keep it: name the variable
`ATHENA_..._OUTPUT_LOCATION` and put the prefix in the template.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# Env-var names that carry an Athena output location. Matched rather than
# listed, so a new consumer following the convention is picked up.
_OUTPUT_VAR = re.compile(r"^ATHENA_[A-Z0-9_]*OUTPUT_LOCATION$")

# ``s3://${SomeVar}/the/prefix/`` → ``the/prefix/``
_S3_URI = re.compile(r"^s3://\$\{[^}]+\}/(?P<prefix>.*)$")

# Non-vacuity pin. These two prefixes exist in the tree today and the
# derivation below must find both. Without this, a parser that stops matching
# anything makes every assertion here pass over an empty set — which is the
# failure mode a gate like this is most likely to develop, and the one that
# would re-open the defect it was written for.
_PREFIXES_PRESENT_TODAY = {"athena-results/", "athena_query_results/"}


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _discovered_templates() -> list[str]:
    """Templates by CONTENT, via the same script the cfn gates use.

    Not a glob and not a list: `make cfn-lint` and `make check-arn-partitions`
    both read this script precisely so a template cannot be added without being
    covered, and this gate joins them rather than keeping a second inventory
    that would drift.
    """
    out = subprocess.run(  # noqa: S603
        [str(REPO_ROOT / "scripts" / "discover_templates.sh"), "cfn"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    paths = [line.strip() for line in out.stdout.splitlines() if line.strip()]
    assert paths, "template discovery returned nothing — this gate would be vacuous"
    return paths


def _refs(expr) -> set[str]:
    """Every name an expression could resolve to.

    An output location's bucket is typically
    ``!If [ShouldCreateReportingBucket, !Ref ReportingBucket,
    !Ref ReportingBucketName]`` — a resource on one branch and a parameter on
    the other — so both branches are collected and either may satisfy the
    check. ``GetAtt`` contributes its logical id.

    ⚠️ **Both spellings of every intrinsic, which is not cosmetic.** The
    long form in JSON-style YAML is ``{"Ref": "X"}``, but ``_CfnLoader`` turns
    the short form ``!Ref X`` into ``{"Fn::Ref": "X"}`` — the multi-constructor
    prefixes every tag with ``Fn::``, including the two that have no ``Fn::``
    spelling in CloudFormation itself. Every template in this repository uses
    the short form, so a version of this function matching only ``Ref`` returns
    an empty set for all of them — and the failure is silent in the worst
    direction: the bucket looks unresolvable, the caller falls through to the
    weaker repo-wide branch, and the specific-bucket assertion never runs.
    ``test_the_derivation_finds_the_prefixes_that_exist_today`` is what refuses
    that state.
    """
    found: set[str] = set()
    if isinstance(expr, str):
        return found
    if isinstance(expr, list):
        for item in expr:
            found |= _refs(item)
        return found
    if isinstance(expr, dict):
        for key, value in expr.items():
            if key in ("Ref", "Fn::Ref") and isinstance(value, str):
                found.add(value)
            elif key in ("GetAtt", "Fn::GetAtt"):
                parts = value.split(".") if isinstance(value, str) else value
                if parts:
                    found.add(str(parts[0]))
            else:
                found |= _refs(value)
    return found


def _output_locations(doc: dict) -> list[tuple[str, str, set[str]]]:
    """``(var_name, prefix, bucket_candidates)`` for each Athena output location."""
    results: list[tuple[str, str, set[str]]] = []
    for body in (doc.get("Resources") or {}).values():
        if not isinstance(body, dict):
            continue
        props = body.get("Properties") or {}
        env = ((props.get("Environment") or {}).get("Variables")) or {}
        if not isinstance(env, dict):
            continue
        for var, value in env.items():
            if not _OUTPUT_VAR.match(str(var)):
                continue
            sub = value.get("Fn::Sub") if isinstance(value, dict) else None
            if isinstance(sub, list) and sub:
                template_str, mapping = sub[0], (sub[1] if len(sub) > 1 else {})
                buckets = _refs(mapping)
            elif isinstance(sub, str):
                template_str, buckets = sub, set()
            else:
                # A location built some other way. Reported rather than
                # skipped silently: the derivation above is the whole gate, so
                # a form it cannot read must be visible.
                results.append((str(var), "<unparseable>", set()))
                continue
            match = _S3_URI.match(str(template_str))
            if not match:
                results.append((str(var), "<unparseable>", buckets))
                continue
            results.append((str(var), match.group("prefix"), buckets))
    return results


def _lifecycle_prefixes(doc: dict) -> dict[str, set[str]]:
    """``{bucket_logical_id: {enabled rule prefixes}}``.

    A rule with no ``Prefix`` covers the whole bucket, recorded as ``""`` so the
    prefix-of test below treats it as covering everything. A disabled rule
    deletes nothing and is not coverage.
    """
    out: dict[str, set[str]] = {}
    for name, body in (doc.get("Resources") or {}).items():
        if not isinstance(body, dict) or body.get("Type") != "AWS::S3::Bucket":
            continue
        rules = (
            (body.get("Properties") or {}).get("LifecycleConfiguration") or {}
        ).get("Rules") or []
        prefixes: set[str] = set()
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("Status") != "Enabled":
                continue
            if rule.get("ExpirationInDays") is None:
                # Transitions and abort-multipart rules delete nothing.
                continue
            prefixes.add(str(rule.get("Prefix") or ""))
        out[name] = prefixes
    return out


def _covers(rule_prefix: str, location_prefix: str) -> bool:
    """Does a lifecycle rule's ``Prefix`` cover an Athena output prefix?

    S3 matches a rule's ``Prefix`` as a literal string prefix of the key, so
    ``athena-`` does cover ``athena-results/`` and is accepted.

    ⚠️ **An empty ``Prefix`` is refused, and that is the whole point.** A rule
    with no prefix covers the entire bucket — which is what
    `DataRetentionInDays` already is. Accepting it here would satisfy every
    Athena prefix whether or not any Athena-scoped rule existed, so the gate
    would pass in exactly the state it exists to reject. One shared predicate
    for both assertions below, so neither can drift from this rule.
    """
    return bool(rule_prefix) and location_prefix.startswith(rule_prefix)


def _parse_all() -> tuple[list[tuple[str, str, str, set[str]]], dict[str, dict]]:
    """``([(template, var, prefix, buckets)], {template: {bucket: prefixes}})``."""
    locations: list[tuple[str, str, str, set[str]]] = []
    coverage: dict[str, dict] = {}
    for rel in _discovered_templates():
        path = REPO_ROOT / rel
        try:
            doc = yaml.load(path.read_text(), Loader=_CfnLoader) or {}
        except yaml.YAMLError:
            # Not this gate's job to police parseability — cfn-lint does that.
            continue
        if not isinstance(doc, dict):
            continue
        for var, prefix, buckets in _output_locations(doc):
            locations.append((rel, var, prefix, buckets))
        coverage[rel] = _lifecycle_prefixes(doc)
    return locations, coverage


_LOCATIONS, _COVERAGE = _parse_all()
_ALL_COVERED = {
    p for buckets in _COVERAGE.values() for ps in buckets.values() for p in ps
}


@pytest.mark.unit
def test_the_derivation_finds_the_prefixes_that_exist_today():
    """Non-vacuity. Every assertion below quantifies over what was discovered,
    so a derivation that finds nothing passes them all while checking nothing.
    """
    found = {prefix for _t, _v, prefix, _b in _LOCATIONS}
    assert "<unparseable>" not in found, (
        "an Athena output location could not be parsed, so its prefix is "
        "unchecked:\n"
        + "\n".join(f"  {t}: {v}" for t, v, p, _b in _LOCATIONS if p == "<unparseable>")
    )
    missing = _PREFIXES_PRESENT_TODAY - found
    assert not missing, (
        f"the derivation no longer finds {sorted(missing)}, which the tree does "
        "contain — the parser or the env-var naming convention changed, and "
        "this gate is now weaker than it looks. Fix the derivation; do not "
        "shrink this set."
    )
    assert _ALL_COVERED, "no lifecycle rules were discovered at all"

    # Bucket resolution must work, not merely not crash. There are two
    # assertions below — a strict one against the bucket a location names, and
    # a weaker repo-wide one for a bucket that arrives as a parameter — and a
    # resolution failure routes EVERY location to the weak branch, turning the
    # strict assertion off with nothing failing. The live hazard is the
    # intrinsic spelling: the loader renders `!Ref X` as `Fn::Ref`, so a
    # `_refs` that matches only `Ref` resolves nothing in a repo whose
    # templates are all short-form.
    resolved_locally = [
        (t, v)
        for t, v, _p, buckets in _LOCATIONS
        if any(name in (_COVERAGE.get(t) or {}) for name in buckets)
    ]
    assert resolved_locally, (
        "no Athena output location resolved to a bucket declared in its own "
        "template, so the strict per-bucket assertion is never exercised and "
        "every location falls to the weaker repo-wide check. Intrinsic "
        "resolution in _refs() is broken — note the loader spells short-form "
        "!Ref as 'Fn::Ref'."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("template", "var", "prefix", "buckets"),
    [pytest.param(*loc, id=f"{loc[0]}::{loc[1]}") for loc in _LOCATIONS],
)
def test_every_athena_output_prefix_is_covered_by_a_lifecycle_rule(
    template, var, prefix, buckets
):
    """The gate: an Athena prefix with no expiring rule scoped to it fails."""
    own = _COVERAGE.get(template) or {}
    local = [name for name in buckets if name in own]

    if local:
        # The bucket is declared in this template, so check that bucket.
        for name in local:
            covering = [p for p in own[name] if _covers(p, prefix)]
            assert covering, (
                f"{template}: {var} writes to '{prefix}' in bucket '{name}', "
                f"which has no expiring lifecycle rule scoped to that prefix "
                f"(its scoped rules: {sorted(p for p in own[name] if p) or 'none'}). "
                "Athena output is scratch and nothing else deletes it. Add a "
                "rule for this prefix, or point the consumer at a prefix "
                "already covered."
            )
        return

    # Bucket arrives as a parameter or an import — see the bound in the module
    # docstring. The prefix must still be covered somewhere in the repo.
    covering = [p for p in _ALL_COVERED if _covers(p, prefix)]
    assert covering, (
        f"{template}: {var} writes to '{prefix}' in a bucket this template "
        f"receives rather than declares ({sorted(buckets) or 'unresolved'}), and "
        "no template in the repo declares an expiring lifecycle rule covering "
        f"that prefix (covered prefixes: {sorted(p for p in _ALL_COVERED if p)})."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rule_prefix", "location_prefix", "covered"),
    [
        # A catch-all rule is NOT coverage. This is the case that matters:
        # `DataRetentionInDays` has no Prefix, so accepting an empty one would
        # satisfy every Athena prefix whether or not an Athena-scoped rule
        # existed — the gate would pass in the state it exists to reject.
        ("", "athena-results/", False),
        ("", "athena_query_results/", False),
        # Exact and genuine string prefixes are coverage, matching how S3
        # evaluates a rule's Prefix against a key.
        ("athena-results/", "athena-results/", True),
        ("athena-", "athena-results/", True),
        # The hyphen/underscore distinction this gate exists for: neither
        # spelling covers the other. ⚠️ The first pair below differs ONLY by
        # the separator, which is what makes it load-bearing — the real
        # prefixes also differ in a word, so a matcher that normalised `_` to
        # `-` would still return False for them and the table would pass while
        # pinning nothing.
        ("athena-results/", "athena_results/", False),
        ("athena_results/", "athena-results/", False),
        ("athena-results/", "athena_query_results/", False),
        ("athena_query_results/", "athena-results/", False),
        # A longer rule prefix does not cover a shorter location.
        ("athena-results/deep/", "athena-results/", False),
        # An unrelated prefix covers nothing.
        ("mcp-temp/", "athena-results/", False),
    ],
)
def test_coverage_requires_a_scoped_prefix(rule_prefix, location_prefix, covered):
    """The predicate both assertions share, tested directly.

    Asserting the predicate's own table rather than the tree means this test
    fails when `_covers` stops refusing an empty prefix, regardless of what
    rules `ReportingBucket` happens to carry. A tree-shaped version of this
    check cannot do that: while the bucket has any scoped rule, no Athena
    prefix depends on the catch-all, so the condition the test name describes
    is unreachable and dropping the empty-prefix guard from the matcher leaves
    the suite green.
    """
    assert _covers(rule_prefix, location_prefix) is covered
