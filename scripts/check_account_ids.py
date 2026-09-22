#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fail if a private AWS account id is committed in a tracked file's CONTENTS.

WHY THIS IS A CONTENT GATE. ``scripts/hooks/check_commit_text.py`` already denies a
``git commit`` or ``gh pr create`` whose *command text* carries an internal address,
hostname or identifier. It could not have caught this class however good its patterns
were: an account id inside a file never appears in the invocation that commits it. A
12-digit id reached two published benchmark release pages, a published planning
document, two skill files, three benchmark ``FINDINGS.md`` files, a unit-test fixture
and four notebooks' saved cell outputs, and was found months later by accident
(issue #1067). So this reads the files.

An account id is not a credential and knowing one grants nothing by itself. What it
does is name a target: it is the input to ``arn:aws:iam::<id>:role/...`` guesses, to
cross-account trust probing, and to enumerating public artifacts named
``<something>-<id>``. In a public repository that is a small, permanent increase in
attack surface for no reader benefit, and the reasoning ``CLAUDE.md`` already records
for commit messages applies with more force to a tracked file, which everyone who
clones reads.

HOW A 12-DIGIT NUMBER IS JUDGED. ``\\b\\d{12}\\b`` alone is a weak signal and this tree
contains ~716 of them, almost all benign. Each hit is resolved in three stages, and the
gate fails only on what survives all three:

1. :data:`ACCOUNT_ID_EXCLUDED_SHAPES` — the run is not a standalone 12-digit number at
   all. A decimal fraction (``1128.611111111111``, ``99.781982421875``: ``.`` is a word
   boundary, so ``\\b`` falls right after it) and the final group of an RFC-4122 UUID
   (``3b3f5985-c36e-4025-ac66-225825306913``) are both judged from the characters around
   the run, per occurrence.
2. :data:`ACCOUNT_ID_ALLOWLIST` — the run *is* a 12-digit id, and that id is not
   private: AWS's own canonical documentation examples, a hand-typed test fixture, an
   AWS-published service account, or a synthetic *bank* account number that is not an
   AWS account at all.
3. :data:`ACCOUNT_ID_EXEMPT_LINES` — the run is a real private id on a line where it is
   load-bearing configuration, so removing it is a behaviour change rather than a
   redaction. Per ``<path>:<line-pattern>``, count-pinned, and deliberately naming no id.

Anything else is unaccounted for and fails. That last word is the design: the universe
is derived from the tree rather than declared, so a brand-new id nobody has classified
is a failure by default instead of joining the backlog.

Exit codes:
    0 — every 12-digit run in every tracked text file is accounted for
    1 — an unaccounted id, a stale entry, or an exempt line that has grown
    2 — the gate could not measure the tree (no git, no tracked files)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A standalone run of exactly 12 digits. Bounded by an explicit "no digit either side"
#: rather than ``\b`` so that a longer digit run cannot contribute a match from its
#: interior: ``\b\d{12}\b`` already cannot, but spelling it out is what makes the
#: decimal-fraction rule below readable — ``.`` is a non-word character, so ``\b`` DOES
#: fall between the point and the digits, which is the whole reason floats false-positive.
ACCOUNT_ID_RE = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")

#: Canonical RFC-4122 shape. Only the fifth group is 12 wide, so a UUID can contribute
#: exactly one candidate, but containment is tested generically rather than assuming that.
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _is_decimal_fraction(line: str, start: int, end: int) -> bool:
    """The run is the fractional part of a decimal number.

    ``1128.611111111111`` and ``99.781982421875`` are OCR confidences and cache deltas,
    and there are five such values in this tree. The test is deliberately narrower than
    "preceded by a point": a point alone also precedes a version segment and a
    dotted path component, and requiring a digit *before* the point means only an
    arithmetic literal is judged benign here.
    """
    return start >= 2 and line[start - 1] == "." and line[start - 2].isdigit()


def _in_uuid(line: str, start: int, end: int) -> bool:
    """The run lies inside a canonical UUID on this line.

    A UUID's last group is 12 hex characters and is all-numeric often enough to matter:
    16 of this tree's candidates are Cognito ``sub`` fixtures of the form
    ``11111111-2222-3333-4444-555555555555``, and the nil UUID supplies another. The
    character immediately before is ``-``, which cannot itself be the discriminator,
    because a bucket named ``idp-notebook-output-<id>-us-west-2`` is the single most
    common way a REAL id appears here.
    """
    return any(m.start() <= start and end <= m.end() for m in UUID_RE.finditer(line))


#: Minimum length of a hexadecimal run for :func:`_in_hex_digest` to call it a digest.
#: 16 is below the shortest digest this tree actually contains (MD5, 32) and well above
#: the 12 an account id occupies, so the rule cannot be satisfied by an id with one
#: stray hex letter beside it. Deliberately conservative: a 12-digit id sitting inside a
#: 13-to-15-character hex token is still reported.
_MIN_DIGEST_LEN = 16


def _in_hex_digest(line: str, start: int, end: int) -> bool:
    """The run is a slice of a longer hexadecimal digest.

    This is the largest false-positive class in this tree and the least obvious one. The
    benchmark corpus names documents by MD5 --
    ``033f718b16cb597c065930410752c294.pdf`` -- and one of those digests contains a
    12-digit run bounded by hex letters on both sides. It appears in every per-document
    row of every benchmark ``summary.csv`` and ``summary.json``, which is 380 of the 392
    candidates a first version of this gate reported.

    The rule expands over hex characters in both directions and requires the maximal run
    to be long enough to be a digest AND to contain a hex *letter*. The letter is what
    makes it safe: a real id in a bucket name or an ARN is delimited by ``-`` or ``:``,
    neither of which is hex, so its maximal hex run is the 12 digits themselves and
    carries no letter.
    """
    hex_chars = "0123456789abcdefABCDEF"
    left = start
    while left > 0 and line[left - 1] in hex_chars:
        left -= 1
    right = end
    while right < len(line) and line[right] in hex_chars:
        right += 1
    run = line[left:right]
    return len(run) >= _MIN_DIGEST_LEN and any(c.isalpha() for c in run)


#: Shape rules, in the order applied. Each takes one occurrence and judges it from the
#: surrounding characters, so the verdict is a property of that occurrence and not of the
#: file or the value. Every rule must currently fire somewhere: a rule that matches
#: nothing is dead and pre-approves whatever next occupies its shape, so the ratchet
#: below fails on one.
ACCOUNT_ID_EXCLUDED_SHAPES = {
    "decimal-fraction": _is_decimal_fraction,
    "uuid-group": _in_uuid,
    "hex-digest": _in_hex_digest,
}

#: 12-digit ids that are genuinely not private, keyed by the id, one reason each.
#:
#: Keyed by VALUE rather than by path on purpose. "Is this id public?" is a property of
#: the id, so one entry states it once and covers every present and future occurrence,
#: and a reviewer sees the claim rather than a list of files. Contrast
#: :data:`ACCOUNT_ID_EXEMPT_LINES`, where the claim is about a line.
ACCOUNT_ID_ALLOWLIST = {
    "123456789012": (
        "AWS's canonical example account id, used throughout AWS's own published "
        "documentation. It is the placeholder this repository substitutes when a "
        "string has to stay a syntactically valid account id."
    ),
    "111122223333": (
        "AWS's second canonical example account id, used for the 'other account' in "
        "cross-account documentation and in this repository's seller/entitlement tests."
    ),
    "999988887777": (
        "AWS's third canonical example account id, used once as an expected-account "
        "argument in a seller-preflight test."
    ),
    "753240598075": (
        "PUBLIC AWS service account: the publisher of the Lambda Web Adapter layer. "
        "template.yaml documents it inline, docs/govcloud-architecture.md names it as "
        "commercial-partition-only, and lib/idp_sdk's template transform carries it as "
        "a code constant that two GovCloud unit tests assert on. It is not ours and "
        "cannot be redacted."
    ),
    "210987654321": (
        "Hand-typed descending digit run, used as the account segment of a Lambda ARN "
        "in two pricing/hook test fixtures. Not an allocated id."
    ),
    "111111111111": "Hand-typed repeated-digit fixture account in the entitlement tests.",
    "222222222222": "Hand-typed repeated-digit fixture account in the entitlement tests.",
    "333333333333": "Hand-typed repeated-digit fixture account in the entitlement tests.",
    "999999999999": (
        "Hand-typed repeated-digit fixture buyer account in one entitlement test body."
    ),
    "000000000000": (
        "All-zero account segment, used where an ARN needs a syntactically valid "
        "account and the value is irrelevant to what is being tested."
    ),
    # The three below are not AWS account ids at all -- they are bank account numbers in
    # synthetic documents. They are listed here rather than as a shape rule because
    # nothing about the surrounding characters distinguishes them; what makes them benign
    # is what the document is, which only a reader can say.
    "000123456789": (
        "Synthetic BANK account number emitted by the benchmark bank-statement "
        "generator, not an AWS account. docs/benchmarking/releases/v0.6.0.md names it "
        "as one of the generator's placeholder values."
    ),
    "003525801543": (
        "Synthetic BANK account number in the rvl-cdip few-shot example's expected "
        "output, alongside AWS's fictitious-name conventions. Not an AWS account."
    ),
    "352580154336": (
        "The second synthetic BANK account number in that same few-shot expected "
        "output. Not an AWS account."
    ),
    "449538294323": (
        "The digits of a synthetic CUSIP security identifier ('F449538294323') in the "
        "1099 ground-truth fixtures. A CUSIP is 9 characters, so this is document "
        "content rather than an identifier of any kind -- not an AWS account."
    ),
    "202606120901": (
        "A YYYYMMDDHHMM build stamp in an illustrative nightly version string "
        "('dev202606120901') in a publish.py comment. Not an AWS account."
    ),
}

#: Lines where a real private account id remains, because on that line it is
#: configuration rather than prose. Entries are ``<path>:<line-pattern>`` -- the shape
#: ``ARN_PARTITION_EXEMPT`` and ``scripts/sdlc/retired_services.json`` already use.
#:
#: Two properties of this list are deliberate.
#:
#: It names no account id. An entry keyed by the id would make this file itself a fresh
#: occurrence in a new commit, which is the exact act the gate exists to stop. The
#: pattern names the surrounding CODE instead -- an environment-variable name, a bucket
#: prefix, a resource kind.
#:
#: Each entry is count-pinned by ``sites``, the number of occurrences it shielded when it
#: was written. Non-vacuity alone would let a file inside an exempt path accumulate more
#: ids behind a reason that was audited against one. Growth fails; shrinking to zero also
#: fails, because a dead entry is a standing licence for whatever next occupies the line.
#:
#: Every one of these is a residual, not a resolution. Redacting any of them changes
#: behaviour -- a CI default, an IAM trust principal, a model ARN in a shipped preset --
#: so each needs an owner decision and its own change, and none belongs in a redaction.
ACCOUNT_ID_EXEMPT_LINES = {
    ".gitlab-ci.yml:IDP_ACCOUNT_ID": {
        "sites": 2,
        "reason": (
            "The CI account id is the documented default of the IDP_ACCOUNT_ID "
            "variable, so the pipeline runs without it being set. Removing the default "
            "makes the variable mandatory in the pipeline configuration, which is a CI "
            "behaviour change and needs the pipeline's owner."
        ),
    },
    "scripts/sdlc/codebuild_deployment.py:IDP_ACCOUNT_ID": {
        "sites": 1,
        "reason": (
            "Same default, in the CodeBuild deployment helper. Dropping it turns a "
            "working invocation into one that fails until the environment is set."
        ),
    },
    "scripts/sdlc/integration_test_deployment.py:IDP_ACCOUNT_ID": {
        "sites": 2,
        "reason": (
            "Same default, twice, in the integration-test deployment helper. Same "
            "consequence: the fallback is what lets the helper run unconfigured."
        ),
    },
    "scripts/sdlc/tests/test_credential_refresh.py:def __init__(self, account=": {
        "sites": 1,
        "reason": (
            "The fake STS client's default account in this test mirrors the CI account "
            "the helper above defaults to. It should become a placeholder in the same "
            "change that parameterises the helper, so the pair stays consistent."
        ),
    },
    "scripts/sdlc/tests/test_stale_bucket_reaper.py:genaiic-sdlc-sourcecode-": {
        "sites": 1,
        "reason": (
            "A real source bucket name, which embeds the CI account id, used as "
            "reaper input. It changes with the bucket-name default above."
        ),
    },
    "scripts/sdlc/cfn/credential-vendor.yml:gitlab-runners-prod": {
        "sites": 2,
        "reason": (
            "Two IAM trust statements name a role in the account that owns the shared "
            "CI runners. The id IS the trust relationship -- a placeholder would grant "
            "trust to a different account -- so it cannot be redacted at all. The same "
            "two lines are exempted from the ARN-partition gate, for the related "
            "reason that cross-partition IAM trust does not exist."
        ),
    },
    "config_library/unified/docsplit/docsplit_finedtuned_config.yaml:custom-model-deployment": {
        "sites": 1,
        "reason": (
            "A Bedrock custom-model-deployment ARN in a shipped preset. The ARN only "
            "resolves in the account that owns the deployment, so redacting the id "
            "without also replacing the model reference leaves a preset that fails at "
            "run time instead of one that fails for other readers only."
        ),
    },
    "config_library/unified/ocr-benchmark/fine_tuned_config.yaml:custom-model-deployment": {
        "sites": 2,
        "reason": (
            "Two more custom-model-deployment ARNs in the OCR benchmark preset, with "
            "the same constraint as the docsplit preset above."
        ),
    },
    "config_library/unified/ocr-benchmark/ocr_fine_tuned_config.yaml:custom-model-deployment": {
        "sites": 2,
        "reason": (
            "The same two ARNs in the sibling OCR preset. Listed separately because a "
            "reason bounded to one file is what makes a mismatch visible while it is "
            "being written."
        ),
    },
}


@dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    line: str

    def render(self) -> str:
        return f"  {self.path}:{self.line_no}: {self.line.strip()[:160]}"


def tracked_files() -> list[str]:
    """Repo-relative paths git would consider committing.

    Asking git rather than walking is the convention ``scripts/discover_templates.sh``
    already uses, and it is load-bearing here for two reasons. ``.gitignore`` is
    honoured, so build output and the agent worktrees under ``.claude/worktrees/`` are
    excluded by the same mechanism CI uses -- a walking gate would report findings
    against a sibling checkout's copy of this tree. And the paths come back relative to
    the checkout, so a checkout's own location cannot exclude it: two gates here used to
    return nothing when run from a worktree under ``.claude/``.

    ``--others --exclude-standard`` includes files that exist but are not committed yet,
    so the verdict does not change at ``git add`` time.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"ERROR: `git ls-files` failed in {REPO_ROOT}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return []
    return sorted({p for p in result.stdout.split("\0") if p})


def _read_text(path: Path) -> str | None:
    """File contents, or ``None`` if it is binary or unreadable.

    Binary files are skipped rather than decoded. A PDF and a TrueType font in this tree
    contain 12-digit runs in content streams and glyph tables, and treating those bytes
    as text produces findings that can never be acted on. It is a real residual and is
    reported as one: an id embedded in a PDF, an image or a parquet file is not detected
    by this gate.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _shape_verdict(line: str, start: int, end: int) -> str | None:
    for name, rule in ACCOUNT_ID_EXCLUDED_SHAPES.items():
        if rule(line, start, end):
            return name
    return None


def _exempt_key(rel_path: str, line: str) -> str | None:
    for key in ACCOUNT_ID_EXEMPT_LINES:
        path, _, pattern = key.partition(":")
        if rel_path == path and pattern in line:
            return key
    return None


@dataclass
class Scan:
    findings: list[Finding]
    shape_hits: dict[str, int]
    allowlist_hits: dict[str, int]
    exempt_hits: dict[str, int]
    files_read: int
    files_skipped_binary: int


def scan(paths: list[str] | None = None, root: Path | None = None) -> Scan:
    base = root or REPO_ROOT
    rels = paths if paths is not None else tracked_files()
    result = Scan([], dict.fromkeys(ACCOUNT_ID_EXCLUDED_SHAPES, 0), {}, {}, 0, 0)
    for rel in rels:
        text = _read_text(base / rel)
        if text is None:
            result.files_skipped_binary += 1
            continue
        result.files_read += 1
        if not ACCOUNT_ID_RE.search(text):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for match in ACCOUNT_ID_RE.finditer(line):
                token = match.group(0)
                shape = _shape_verdict(line, match.start(), match.end())
                if shape is not None:
                    result.shape_hits[shape] += 1
                    continue
                if token in ACCOUNT_ID_ALLOWLIST:
                    result.allowlist_hits[token] = (
                        result.allowlist_hits.get(token, 0) + 1
                    )
                    continue
                key = _exempt_key(rel, line)
                if key is not None:
                    result.exempt_hits[key] = result.exempt_hits.get(key, 0) + 1
                    continue
                result.findings.append(Finding(rel, line_no, line))
    return result


def _ratchet_problems(result: Scan) -> list[str]:
    """Stale, dead and grown entries. Every carve-out here is finite or it fails.

    Three directions, and the third is the one a plain allowlist never has. A shape rule
    or an allowlist entry that matches nothing is *vacuous* -- it shields no finding
    today, so it has no expressible reason and it pre-approves whatever next occupies
    that shape or value. An exempt line whose occurrence count has *grown* past what was
    audited has outrun its reason, which is the failure mode of every per-directory
    exemption this repository has had to unpick.
    """
    problems: list[str] = []
    for name, hits in result.shape_hits.items():
        if hits == 0:
            problems.append(
                f"shape rule {name!r} in ACCOUNT_ID_EXCLUDED_SHAPES matched nothing, so "
                "it is dead and silently pre-approves that shape. Delete it."
            )
    for token, reason in ACCOUNT_ID_ALLOWLIST.items():
        if result.allowlist_hits.get(token, 0) == 0:
            problems.append(
                f"ACCOUNT_ID_ALLOWLIST entry for a {len(token)}-digit id hides nothing "
                f"(reason on file: {reason[:60]}...). It is stale -- delete it, so it "
                "cannot pre-approve that value returning for a different purpose."
            )
    for key, entry in ACCOUNT_ID_EXEMPT_LINES.items():
        seen = result.exempt_hits.get(key, 0)
        pinned = entry["sites"]
        if seen == 0:
            problems.append(
                f"ACCOUNT_ID_EXEMPT_LINES entry {key!r} shields nothing. The id was "
                "redacted or the line moved: delete the entry."
            )
        elif seen != pinned:
            problems.append(
                f"ACCOUNT_ID_EXEMPT_LINES entry {key!r} now shields {seen} "
                f"occurrence(s), pinned at {pinned}. A new occurrence inside an already "
                "exempt path is not covered by the audit that produced that number: "
                "redact the new one, or re-pin deliberately and say why in the reason."
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print what each stage accounted for, then the verdict",
    )
    args = parser.parse_args(argv)

    paths = tracked_files()
    if not paths:
        print(
            "ERROR: no tracked files discovered, so this gate proves nothing. Check "
            f"that {REPO_ROOT} is a git checkout.",
            file=sys.stderr,
        )
        return 2

    result = scan(paths)

    if args.summary:
        print(
            f"Read {result.files_read} tracked text files "
            f"({result.files_skipped_binary} binary skipped)."
        )
        print("Accounted for by shape (not a 12-digit id at all):")
        for name, hits in sorted(result.shape_hits.items()):
            print(f"  {name}: {hits}")
        print(
            f"Accounted for by ACCOUNT_ID_ALLOWLIST: "
            f"{sum(result.allowlist_hits.values())} occurrence(s) across "
            f"{len(result.allowlist_hits)} id(s)"
        )
        print(
            f"Accounted for by ACCOUNT_ID_EXEMPT_LINES: "
            f"{sum(result.exempt_hits.values())} occurrence(s) across "
            f"{len(result.exempt_hits)} line(s)"
        )

    problems = _ratchet_problems(result)
    exit_code = 0

    if result.findings:
        exit_code = 1
        print(
            f"ERROR: {len(result.findings)} unaccounted 12-digit account id "
            f"occurrence(s) in tracked files:",
            file=sys.stderr,
        )
        for finding in result.findings:
            print(finding.render(), file=sys.stderr)
        print(
            "\nEach of these is neither a recognised non-account shape, nor an "
            "allowlisted public/synthetic id, nor an exempt configuration line.\n"
            "  If it is a PRIVATE account id: redact it. Published prose does not need "
            "it; an instruction to an assistant takes <ACCOUNT_ID>; a value that must "
            "stay syntactically valid takes a documentation placeholder.\n"
            "  If it is public or synthetic: add it to ACCOUNT_ID_ALLOWLIST with the "
            "reason it is not private.\n"
            "  If it is private but load-bearing: add a <path>:<line-pattern> entry to "
            "ACCOUNT_ID_EXEMPT_LINES with the reason and the site count.\n"
            "See issue #1067, and the exemption rules in CLAUDE.md.",
            file=sys.stderr,
        )

    if problems:
        exit_code = 1
        print(
            f"\nERROR: {len(problems)} exemption ratchet problem(s):", file=sys.stderr
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)

    if exit_code == 0:
        print(
            f"✅ No unaccounted account ids in {result.files_read} tracked text files."
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
