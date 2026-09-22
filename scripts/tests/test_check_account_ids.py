# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Behaviour of the committed-account-id gate (``scripts/check_account_ids.py``).

The gate's value rests entirely on its discriminating rule, so this drives that rule
from both sides: an id planted in each of the file kinds the exposure actually used must
be reported, and each false-positive shape the tree already contains must not be. A
detector that fires on everything gets turned off, and one that fires on nothing is the
condition it was written to end.

The synthetic-tree cases pass ``paths`` and ``root`` explicitly rather than letting the
gate ask git, so they measure the classification and not the discovery. Discovery is
covered separately, against the real checkout.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "scripts" / "check_account_ids.py"


def _load_gate():
    """Import the gate by path.

    ``scripts/`` is not a package, so this is how the other gate tests here reach one.
    The module is registered in ``sys.modules`` before execution because ``@dataclass``
    resolves annotations through ``sys.modules[cls.__module__]`` and raises if the
    module it names is absent.
    """
    spec = importlib.util.spec_from_file_location("check_account_ids", GATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

#: Probe values that are in none of the gate's three accounted-for sets, so each stands
#: in for "an account id nobody has classified".
#:
#: Assembled from halves rather than written as literals, and the reason is worth stating
#: because it is easy to undo. This file is itself a tracked file that the gate scans, so
#: a literal here would be an unaccounted id and the gate would report its own test
#: suite — which it did, on the first run. Allowlisting them instead would be worse: the
#: "it fires" cases below assert that an unclassified id is reported, and the allowlist is
#: exactly the set they must not be in, so allowlisting would make them pass vacuously.
PLANTED = "487391" + "026584"
SECOND = "487391" + "026585"
NEVER_IN_THE_TREE = "487391" + "026599"


def _scan_one(tmp_path: Path, rel: str, content: str):
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):  # pyright: ignore[reportUnnecessaryIsInstance]
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return gate.scan(paths=[rel], root=tmp_path)


# --------------------------------------------------------------------------- #
# It fires: an unclassified id in each file kind the real exposure used
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("rel", "content"),
    [
        # Published prose — how it reached the benchmark release pages and FINDINGS.md.
        ("docs/page.md", f"Measured on a stack (us-west-2, account {PLANTED}).\n"),
        # An instruction to an assistant — how it reached two skill files. Note the dot
        # directory: a gate that skipped those would miss this entirely.
        (".claude/skills/thing.md", f"account **{PLANTED}**, region us-west-2\n"),
        # A bucket name, which is how 141 of the 152 occurrences appeared, and the case
        # that rules out "preceded by a hyphen" as a discriminator.
        ("notebooks/n.ipynb", f'"Output bucket: idp-out-{PLANTED}-us-west-2\\n",\n'),
        # A test fixture.
        ("lib/x/tests/test_y.py", f'    sts = _Sts(account="{PLANTED}")\n'),
        # An ARN, in YAML and in a template.
        ("config/c.yaml", f"  model: arn:aws:bedrock:us-east-1:{PLANTED}:foo/bar\n"),
        # Extensionless files: the Makefile carried one, and an extension allowlist
        # would not have looked at it.
        ("Makefile", f"#   make thing SELLER_ACCOUNT_ID={PLANTED} YES=1\n"),
        # CI configuration.
        (".gitlab-ci.yml", f"    - export ACCT=${{ACCT:-{PLANTED}}}\n"),
        # JSON and CSV data, where the benchmark artifacts live.
        ("results/summary.json", f'  "account": "{PLANTED}",\n'),
        ("results/summary.csv", f"run,acct\nfoo,{PLANTED}\n"),
    ],
)
def test_reports_an_unclassified_id(tmp_path: Path, rel: str, content: str) -> None:
    result = _scan_one(tmp_path, rel, content)
    assert [f.path for f in result.findings] == [rel], (
        f"an unclassified 12-digit id in {rel} was not reported; this gate exists "
        "because every one of these file kinds carried one"
    )


def test_counts_both_ids_on_a_line_that_carries_two(tmp_path: Path) -> None:
    """Two ids on one line are two accounted-for occurrences, not one.

    One of the redacted skill-file lines named the account and then embedded it in a
    bucket name on the next line, and the notebook outputs put two bucket names on a
    single line. A scan that stopped at a line's first match would report a line as
    clean once its first id was fixed, and would undercount every ``sites`` pin.
    """
    content = f"bucket b-{PLANTED}-x and account {SECOND}\n"
    result = _scan_one(tmp_path, "a.md", content)
    assert len(result.findings) == 2, (
        "both ids on the line must be reported separately: "
        f"{[f.line for f in result.findings]}"
    )
    assert {f.line_no for f in result.findings} == {1}


# --------------------------------------------------------------------------- #
# It does not fire: the shapes this tree already contains
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "content"),
    [
        # The two shapes named in issue #1067's discussion as certain false positives.
        (
            "float with a 12-digit fraction",
            '  "cache_read_delta": 1128.611111111111,\n',
        ),
        ("OCR confidence", '      "confidence": 99.781982421875\n'),
        ("synthetic bank account", '    "Account Number": "000123456789"\n'),
        # UUID tails, which look exactly like an id in a bucket name apart from context.
        ("cognito sub", '    "sub": "11111111-2222-3333-4444-555555555555"\n'),
        ("nil uuid", '    x = "00000000-0000-0000-0000-000000000000"\n'),
        ("request id", "    'RequestId': '3b3f5985-c36e-4025-ac66-225825306913'\n"),
        # The largest class by volume: MD5-named corpus documents.
        ("md5 document name", "  033f718b16cb597c065930410752c294.pdf,0,COMPLETED\n"),
        # AWS's own documentation placeholders.
        ("aws example account", "  arn:aws:iam::123456789012:role/Thing\n"),
        ("aws second example", '  Default: "111122223333"\n'),
        # A published AWS service account.
        ("lambda web adapter publisher", "  arn:aws:lambda:us-east-1:753240598075:x\n"),
    ],
)
def test_does_not_report_a_known_benign_shape(
    tmp_path: Path, label: str, content: str
) -> None:
    result = _scan_one(tmp_path, "probe.txt", content)
    assert not result.findings, (
        f"{label} was reported as an account id. A gate that false-positives on "
        f"values already in this tree gets switched off: {[f.line for f in result.findings]}"
    )


def test_a_hyphen_before_the_run_is_not_treated_as_benign() -> None:
    """The UUID rule must not degrade into "preceded by a hyphen".

    A UUID's final group and an id inside ``bucket-<id>-region`` both sit immediately
    after a hyphen, and the second is how 141 of the 152 occurrences of the id in
    issue #1067 appeared. So the character before the match cannot be the
    discriminator, and this is the case that fails if it ever becomes one.
    """
    bucket = f"idp-notebook-output-{PLANTED}-us-west-2"
    start = bucket.index(PLANTED)
    assert not gate._in_uuid(bucket, start, start + 12)
    assert gate._shape_verdict(bucket, start, start + 12) is None

    uuid_line = "11111111-2222-3333-4444-555555555555"
    assert gate._shape_verdict(uuid_line, 24, 36) == "uuid-group"


def test_a_decimal_point_alone_is_not_treated_as_benign() -> None:
    """The fraction rule requires a digit before the point, not just a point.

    A dotted path segment and a version component are not arithmetic, so an id written
    after a bare point must still be reported; only a digit-point-digits run is a
    decimal literal.
    """
    line = f"someprefix.{PLANTED} and 99.{PLANTED}"
    first = line.index(PLANTED)
    assert not gate._is_decimal_fraction(line, first, first + 12)
    second = line.index(PLANTED, first + 1)
    assert gate._is_decimal_fraction(line, second, second + 12)


def test_hex_digest_rule_requires_a_letter_and_length(tmp_path: Path) -> None:
    """The digest rule is bounded, so it cannot swallow a real id.

    A 12-digit id delimited by ``-`` or ``:`` has a maximal hex run of exactly 12 with
    no letter in it, so neither condition is met. A CUSIP-like ``F449538294323`` is 13
    characters and also stays reported by shape — it is allowlisted by value instead,
    which is the honest way to record "this is document content".
    """
    assert not gate._in_hex_digest(f"b-{PLANTED}-x", 2, 14)
    assert not gate._in_hex_digest(f"arn:aws:iam::{PLANTED}:role/x", 13, 25)
    assert gate._in_hex_digest("033f718b16cb597c065930410752c294", 16, 28)
    # 13 hex characters with a letter is below the digest threshold, so not excused.
    assert not gate._in_hex_digest("F449538294323", 1, 13)


# --------------------------------------------------------------------------- #
# Binary files
# --------------------------------------------------------------------------- #


def test_skips_binary_files(tmp_path: Path) -> None:
    """A PDF and a font in this tree contain 12-digit runs in their bytes.

    They are skipped rather than decoded, and the skip is counted so the residual is
    visible in ``--summary`` rather than implied.
    """
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\x00 " + PLANTED.encode() + b" \x00")
    result = gate.scan(paths=["doc.pdf"], root=tmp_path)
    assert not result.findings
    assert result.files_skipped_binary == 1
    assert result.files_read == 0


# --------------------------------------------------------------------------- #
# The ratchets
# --------------------------------------------------------------------------- #


def test_a_dead_shape_rule_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shape rule that matches nothing pre-approves whatever next takes that shape."""
    monkeypatch.setitem(
        gate.ACCOUNT_ID_EXCLUDED_SHAPES, "never-fires", lambda *_: False
    )
    result = gate.scan(paths=[], root=REPO_ROOT)
    result.shape_hits.setdefault("never-fires", 0)
    problems = gate._ratchet_problems(result)
    assert any("never-fires" in p and "matched nothing" in p for p in problems)


def test_a_stale_allowlist_entry_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """An allowlisted id that no longer appears is deleted, not left standing."""
    monkeypatch.setitem(gate.ACCOUNT_ID_ALLOWLIST, NEVER_IN_THE_TREE, "not in the tree")
    result = gate.scan(paths=[], root=REPO_ROOT)
    for name in gate.ACCOUNT_ID_EXCLUDED_SHAPES:
        result.shape_hits[name] = 1
    problems = gate._ratchet_problems(result)
    assert any("hides nothing" in p for p in problems)


def test_an_exempt_line_that_grew_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new id inside an already-exempt path is not covered by the audit behind it.

    This is the ratchet the class needs most. Every exemption list this repository has
    had to unpick was one whose reason was measured against fewer members than it came
    to shield.
    """
    key = next(iter(gate.ACCOUNT_ID_EXEMPT_LINES))
    pinned = gate.ACCOUNT_ID_EXEMPT_LINES[key]["sites"]
    result = gate.scan(paths=[], root=REPO_ROOT)
    for name in gate.ACCOUNT_ID_EXCLUDED_SHAPES:
        result.shape_hits[name] = 1
    for token in gate.ACCOUNT_ID_ALLOWLIST:
        result.allowlist_hits[token] = 1
    for other in gate.ACCOUNT_ID_EXEMPT_LINES:
        result.exempt_hits[other] = gate.ACCOUNT_ID_EXEMPT_LINES[other]["sites"]
    result.exempt_hits[key] = pinned + 1
    problems = gate._ratchet_problems(result)
    assert any(key in p and "pinned at" in p for p in problems)


def test_every_exempt_entry_names_a_tracked_file_and_a_pattern_that_matches() -> None:
    """Non-vacuity and shape, per entry, against the real tree.

    An entry naming a path that is gone, or a pattern that matches no line of it, is a
    standing licence for whatever next occupies that path.
    """
    for key, entry in gate.ACCOUNT_ID_EXEMPT_LINES.items():
        path, sep, pattern = key.partition(":")
        assert sep and pattern, f"{key!r} is not <path>:<line-pattern>"
        assert isinstance(entry.get("sites"), int) and entry["sites"] > 0, (
            f"{key}: 'sites' must pin a positive occurrence count"
        )
        assert entry.get("reason", "").strip(), f"{key}: no reason recorded"
        target = REPO_ROOT / path
        assert target.is_file(), f"{key}: {path} is not a file in this checkout"
        text = target.read_text(encoding="utf-8")
        assert pattern in text, f"{key}: no line of {path} contains {pattern!r}"


def test_no_exempt_entry_or_reason_contains_a_12_digit_id() -> None:
    """The gate's own source must not become another occurrence.

    Keying the residual list by id would mean this file republished every id it
    describes, in a fresh commit, which is the act the gate exists to prevent.
    """
    for key, entry in gate.ACCOUNT_ID_EXEMPT_LINES.items():
        blob = f"{key} {entry['reason']}"
        assert not gate.ACCOUNT_ID_RE.search(blob), (
            f"{key} names a 12-digit id. Name the surrounding code — a variable name, a "
            "bucket prefix, a resource kind — not the id."
        )


def test_the_real_tree_is_clean() -> None:
    """The gate passes on this checkout, by discovery rather than a supplied list.

    This is the half that exercises ``tracked_files``, and it fails if discovery ever
    returns nothing — a gate that scans zero files passes vacuously, which two other
    gates in this directory have done from inside an agent worktree.
    """
    result = gate.scan()
    assert result.files_read > 1000, (
        f"discovery read only {result.files_read} files, so this gate proves almost "
        "nothing. Check that it asks git and resolves paths relative to the checkout."
    )
    assert not result.findings, [f.render() for f in result.findings]
    assert not gate._ratchet_problems(result)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def test_the_gate_runs_from_lint_and_from_lint_cicd() -> None:
    """Both CIs run ``make lint-cicd``, so that is what makes this gate blocking.

    ``test_ci_gate_parity.py`` enforces the general rule; this asserts the specific
    wiring, because a gate nothing invokes is indistinguishable from one that passes.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\ncheck-account-ids:" in makefile, "no check-account-ids target"
    lint_line = next(
        line for line in makefile.splitlines() if line.startswith("lint: ")
    )
    assert "check-account-ids" in lint_line
    fastlint_line = next(
        line for line in makefile.splitlines() if line.startswith("fastlint: ")
    )
    assert "check-account-ids" in fastlint_line
    start = makefile.index("\nlint-cicd:")
    end = makefile.index("\ncheck-lint-debt:", start)
    assert "check-account-ids" in makefile[start:end], (
        "check-account-ids is not reached from lint-cicd, so neither CI runs it"
    )


def test_the_gate_is_registered_as_an_exemption_surface() -> None:
    """All three of the gate's carve-outs are in the registry.

    Registering them is what stops the next one being added silently; the meta-test in
    ``test_gate_exemption_registry.py`` enforces it in both directions, and this states
    the expectation locally so a rename that hides a surface from discovery is visible
    here too.
    """
    registry = json.loads(
        (Path(__file__).resolve().parent / "gate_exemptions.json").read_text(
            encoding="utf-8"
        )
    )["exemptions"]
    for name in (
        "ACCOUNT_ID_ALLOWLIST",
        "ACCOUNT_ID_EXEMPT_LINES",
        "ACCOUNT_ID_EXCLUDED_SHAPES",
        "BINARY_EXTENSIONS_SKIPPED",
    ):
        key = f"scripts/check_account_ids.py::{name}"
        assert key in registry, f"{key} is not registered"


# --------------------------------------------------------------------------- #
# Closure over the binary skip
# --------------------------------------------------------------------------- #


def test_an_undeclared_unreadable_file_is_reported(tmp_path: Path) -> None:
    """A file the gate cannot read, in a format nothing declared, must fail.

    This is the direction that matters. A skipped-file *count* would red-line the branch
    on the next sample image; what needs to fail is coverage silently shrinking — a text
    file that gains a NUL byte, or a new tree in some binary format nobody classified.
    """
    (tmp_path / "notes.md").write_bytes(f"account {PLANTED}\x00 trailing\n".encode())
    result = gate.scan(paths=["notes.md"], root=tmp_path)
    assert result.files_skipped_binary == 1
    assert result.unaccounted_skips == ["notes.md"]
    problems = gate._ratchet_problems(result)
    assert any("notes.md" in p and "unaccounted" in p for p in problems), problems


def test_a_declared_binary_extension_is_not_reported(tmp_path: Path) -> None:
    """A genuine image is skipped silently, so the set does not become busywork."""
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00 " + PLANTED.encode())
    result = gate.scan(paths=["shot.png"], root=tmp_path)
    assert result.files_skipped_binary == 1
    assert result.unaccounted_skips == []


def test_every_skipped_file_in_the_real_tree_is_declared() -> None:
    """Closure, against this checkout: 133 files today, every one a declared format."""
    result = gate.scan()
    assert result.files_skipped_binary > 100, (
        f"only {result.files_skipped_binary} files skipped; if binary detection has "
        "changed, re-measure the set rather than assuming it still holds"
    )
    assert result.unaccounted_skips == [], result.unaccounted_skips


def test_the_command_form_exits_0_on_this_clean_checkout() -> None:
    """End to end through the command form, asserting the exit code exactly.

    The Makefile recipe branches on this value, so it is asserted as one number rather
    than a set of acceptable ones — a test that accepts ``0 or 1`` passes whatever
    happens and is indistinguishable from not testing it.

    The two failing exit codes, 1 and 2, are covered by the ``main()`` cases below, which
    can drive them without writing into the repository.
    """
    proc = subprocess.run(
        ["python3", str(GATE_PATH), "--summary"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-2000:]}"
    )
    assert "binary skipped" in proc.stdout
    assert "ACCOUNT_ID_EXEMPT_LINES" in proc.stdout


def test_main_returns_1_on_an_unclassified_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main()`` must return 1, which is the value the Makefile recipe branches on.

    Driven by pointing the gate's root and its discovery at a synthetic tree, so a
    finding can be planted without writing into the repository.
    """
    (tmp_path / "doc.md").write_text(f"account {PLANTED}\n", encoding="utf-8")
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gate, "tracked_files", lambda: ["doc.md"])

    assert gate.main([]) == 1
    err = capsys.readouterr().err
    assert "unaccounted 12-digit account id" in err
    assert "doc.md:1" in err


def test_main_returns_2_when_it_cannot_measure_the_tree(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Discovery returning nothing is a distinct exit code, not a pass.

    A gate that scans zero files and prints success is the failure mode two other gates
    in this directory had from inside an agent worktree, so "I could not measure" must
    not share an exit code with "I measured and it was clean".
    """
    monkeypatch.setattr(gate, "tracked_files", lambda: [])
    assert gate.main([]) == 2
    assert "no tracked files discovered" in capsys.readouterr().err
