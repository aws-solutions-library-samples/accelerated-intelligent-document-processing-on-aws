# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``DockerBuildRun``'s ``ExpectedImages`` against the images the build pushes.

The custom resource waits for every tag in ``ExpectedImages`` to appear in ECR
before the Lambda functions that pull them are created. The tags are produced by
a loop in the buildspec the project points at, and the two lists are maintained
in different files with nothing connecting them.

An entry in ``ExpectedImages`` that the buildspec does not build therefore waits
forever. That is not a hypothetical shape: it is exactly the end state of issue
[#1310](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1310)
(a build that reported success without pushing a tag) and of
[#1336](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1336)
(an unbounded scan wait) -- an hour of polling ending in ``CloudFormation did not
receive a response from your Custom Resource``, which names the custom resource
and not the missing image. Both of those were fixed at their own cause; a rename
or a one-sided addition here reproduces the same hang through a third route, and
the scan wait's bound does not help because presence polling is deliberately
unbounded.

Only the subset direction is asserted. An image the buildspec builds and
``ExpectedImages`` does not name is harmless: nothing waits for it, and two such
images exist today by design.

Both lists are parsed from the files rather than restated here, and both parses
are checked for non-vacuity, because a parse that silently finds nothing would
make the subset assertion trivially true.

Why the template is loaded rather than scanned
----------------------------------------------
The property is read through this repository's single CloudFormation-tolerant
YAML loader, and the whole document is walked for the key. The guarantee this
gate needs is a *capability* one -- the parse cannot fail to see an entry the
template declares -- and a hand-rolled scan cannot have it, because it has to
recognise every spelling YAML permits for a list entry: a comment after a value,
a comment on its own line at any indentation, single and double quoting, a bare
scalar, a ``#`` inside a quoted string, a block scalar. A scan that stops at the
first line it does not recognise reads a prefix and leaves the tail unchecked,
and the three non-vacuity assertions below cannot see that, because a truncated
prefix still satisfies all of them
([#1347](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1347)).
``test_the_parse_sees_every_entry_whatever_its_spelling`` is the measurement of
that capability, driven over entries spliced into the template's own block.

The loader is loaded **by path**, for the same three reasons
``scripts/tests/test_log_group_encryption.py`` gives for doing so: an editable
install can resolve ``idp_sdk`` to a different checkout and parse this tree's
template with another tree's parser; ``idp_sdk._core`` is banned from direct
import by ruff's TID251 outside a listed set of directories, which this one is
not in; and, the strongest, a repository gate should not need an editable install
to be present at all.

An entry the loader yields as anything other than a string -- a ``!Sub``, a
``!Ref``, a block mapping -- is refused rather than skipped. The gate cannot tell
whether such an entry names an image the buildspec builds, so the honest outcome
is a loud failure naming it; silently dropping it would leave exactly the hang
above uncovered for that entry.

One shape is read without complaint and is worth knowing about: a resource
declaring ``ExpectedImages`` **twice** in the same mapping yields only the last
block, because that is what PyYAML does with a duplicate key. ``cfn-lint`` reports
the duplicate at ERROR class (``E0000 Duplicate found``) on a gate that runs in
``lint``, ``fastlint`` and ``lint-cicd``, so it does not go unreported -- and that
gate and this one run in parallel rather than in sequence, so neither is "first".
"""

from __future__ import annotations

import functools
import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_PATTERN_DIR = Path(__file__).resolve().parents[1]
_TEMPLATE = _PATTERN_DIR / "template.yaml"
_REPO_ROOT = _PATTERN_DIR.parents[1]
_CFN_YAML = _REPO_ROOT / "lib" / "idp_sdk" / "idp_sdk" / "_core" / "cfn_yaml.py"

_PROPERTY = "ExpectedImages"


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def _template_text() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=None)
def _cfn_yaml_module() -> Any:
    spec = importlib.util.spec_from_file_location("_cfn_yaml_for_gate", _CFN_YAML)
    assert spec is not None and spec.loader is not None, f"cannot load {_CFN_YAML}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_text(text: str) -> dict:
    """Parse one CloudFormation document, tolerating short-form intrinsics.

    An intrinsic comes back as a single-key ``{"!Tag": value}`` dict, which is
    what lets the refusal below tell a literal image name from a computed one.
    """
    return _cfn_yaml_module().load_cfn_yaml(text) or {}


@functools.lru_cache(maxsize=None)
def _document() -> dict:
    return _load_text(_template_text())


def _values_for_key(node: Any, key: str) -> list[Any]:
    """Every value the document associates with ``key``, at any depth.

    Walking the whole document rather than indexing one logical id is what makes
    a second resource declaring the same property covered without being named
    here. Strings are not walked, so a ``#`` or a ``BuildSpec:`` inside a scalar
    cannot be mistaken for structure.
    """
    found: list[Any] = []
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                found.append(value)
            found.extend(_values_for_key(value, key))
    elif isinstance(node, list):
        for item in node:
            found.extend(_values_for_key(item, key))
    return found


def _buildspec_path_in(document: dict) -> Path:
    """The buildspec the DockerBuildProject actually points at.

    Read from the template rather than hardcoded, so that switching the project
    to one of the sibling buildspecs moves this test with it instead of leaving
    it asserting against a file nothing runs.
    """
    values = _values_for_key(document, "BuildSpec")
    assert values, "no BuildSpec property found in patterns/unified/template.yaml"
    assert all(isinstance(value, str) for value in values), (
        f"a BuildSpec property is not a literal path: {values}. This gate reads the "
        "buildspec from disk, so it cannot follow a computed one."
    )
    assert len(set(values)) == 1, f"more than one BuildSpec in the template: {values}"

    path = _REPO_ROOT / values[0]
    assert path.is_file(), f"BuildSpec names {values[0]}, which does not exist"
    return path


def _buildspec_path() -> Path:
    return _buildspec_path_in(_document())


def _expected_images_in(document: dict) -> list[str]:
    """Every ``ExpectedImages`` entry the document declares, in document order."""
    blocks = _values_for_key(document, _PROPERTY)
    assert blocks, f"no {_PROPERTY} list found in patterns/unified/template.yaml"

    images: list[str] = []
    for block in blocks:
        assert isinstance(block, list), (
            f"{_PROPERTY} is a {type(block).__name__}, not a list: {block!r}. The "
            "custom resource iterates it, so this gate cannot check a computed one."
        )
        for entry in block:
            assert isinstance(entry, str), (
                f"{_PROPERTY} contains {entry!r}, which the loader did not yield as a "
                "literal string -- an intrinsic such as !Sub or !Ref. This gate cannot "
                "tell whether it names an image the buildspec builds, and an entry no "
                "build produces makes the custom resource poll until CloudFormation's "
                "one-hour limit. Either write the tag literally or extend this parse "
                "to resolve it; do not leave it unchecked."
            )
            images.append(entry)
    return images


def _expected_images_in_text(text: str) -> list[str]:
    """The whole parse, from template text to image names.

    The seam the capability probes drive, so that swapping the parse for a
    different one is a single-function change and the probes measure it rather
    than their own plumbing.
    """
    return _expected_images_in(_load_text(text))


def _expected_images() -> list[str]:
    return _expected_images_in(_document())


def _buildspec_commands(buildspec: Path) -> list[str]:
    """Every command string in every phase of the buildspec.

    ``yaml.safe_load`` is correct here: a buildspec carries no CloudFormation
    intrinsics, so no custom loader is involved.
    """
    document = yaml.safe_load(buildspec.read_text(encoding="utf-8"))
    commands: list[str] = []
    for phase in (document.get("phases") or {}).values():
        for command in phase.get("commands") or []:
            if isinstance(command, str):
                commands.append(command)
    return commands


def _image_name_for(function_path: str) -> str:
    """The tag the buildspec derives: ``basename | sed 's/_/-/g'``."""
    return Path(function_path).name.replace("_", "-")


def _exported_functions(commands: list[str]) -> dict[str, str]:
    exports: dict[str, str] = {}
    for command in commands:
        for name, path in re.findall(
            r'export\s+(FUNCTION_[A-Za-z0-9_]+)="([^"]+)"', command
        ):
            exports[name] = path
    return exports


def _looped_functions(commands: list[str]) -> list[str]:
    """The FUNCTION_* variables the build loop iterates over.

    An export that the loop does not name is not built, so the loop -- not the
    export list -- is the universe of images this build produces.
    """
    looped: list[str] = []
    for command in commands:
        for body in re.findall(
            r"for\s+func_var\s+in\s+(.+?);\s*do", command, re.DOTALL
        ):
            looped.extend(re.findall(r"FUNCTION_[A-Za-z0-9_]+", body))
    return looped


def _built_images_from(commands: list[str]) -> frozenset[str]:
    exports = _exported_functions(commands)
    return frozenset(
        _image_name_for(exports[name])
        for name in _looped_functions(commands)
        if name in exports
    )


@functools.lru_cache(maxsize=None)
def _built_images() -> frozenset[str]:
    return _built_images_from(_buildspec_commands(_buildspec_path()))


def _unbuilt(expected: list[str], built: frozenset[str]) -> list[str]:
    """The expected images no build produces.

    A function rather than an expression inlined in the assertion below, so that
    the probes drive the comparison the gate really makes. A probe that
    re-implemented it could not see a prefix bug in the comparison -- which is the
    same defect, in the next step along, as the prefix bug in the parse that this
    module's capability probes exist for.
    """
    return sorted(set(expected) - built)


# --------------------------------------------------------------------------- #
# the parses are not vacuous
# --------------------------------------------------------------------------- #


def test_the_expected_images_list_parses_non_empty() -> None:
    images = _expected_images()
    assert len(images) >= 3, f"{_PROPERTY} parsed as {images}"
    assert all(name.endswith("-function") for name in images), images
    assert len(set(images)) == len(images), f"duplicate entries: {images}"


def test_the_built_image_set_parses_non_empty() -> None:
    built = _built_images()
    assert len(built) >= 3, f"built images parsed as {built}"
    assert all(name.endswith("-function") for name in built), built


def test_every_variable_the_loop_names_is_exported() -> None:
    """An unexported FUNCTION_* yields an empty path, so a tag of just the version.

    Same consequence as the subset failure below -- the expected tag never
    appears -- reached inside the buildspec instead of across the two files.
    """
    commands = _buildspec_commands(_buildspec_path())
    exports = _exported_functions(commands)
    looped = _looped_functions(commands)

    assert looped, "no build loop over FUNCTION_* variables found in the buildspec"
    missing = sorted(name for name in looped if name not in exports)
    assert not missing, f"the build loop names unexported variables: {missing}"


# --------------------------------------------------------------------------- #
# the parse cannot fail to see an entry the template declares
#
# The cases below are spellings YAML permits for a list entry. They are probes,
# not the rule: the rule is that the parse is a YAML parse, so no case in this
# table is special-cased anywhere above. Each one splices a tail onto the real
# block and asserts the whole list -- every real entry plus the injected tail --
# comes back, which is the property a scan that stops at an unrecognised line
# does not have.
# --------------------------------------------------------------------------- #

_SENTINEL = "probe-sentinel-function"
_HASHED_SENTINEL = "probe#sentinel-function"

# One sentinel per splice position, and the prefixes are the point rather than
# decoration: ``_unbuilt`` sorts before it compares, so a comparison that truncates
# the **sorted** list is blind to every splice position unless the sentinel's name
# sorts where it was spliced. ``probe-sentinel-function`` sorts to rank 9 of the 14
# entries -- strictly interior, whatever line it sits on -- so measured,
# ``set(sorted(set(expected))[:-1]) - built`` and ``set(sorted(set(expected))[1:]) -
# built`` both passed all three positions, and a slice landing beside the
# ``sorted(...)`` already in ``_unbuilt`` is a plausible edit. The alphabetically
# last expected image today is ``summarization-function``, so ``zzz-`` sorts after
# every real entry and ``aaa-`` before every one. The rank is asserted in the test
# rather than assumed, so a rename that lands interior fails loudly instead of
# quietly giving the property back.
_POSITION_SENTINELS = {
    "start-of-list": f"aaa-{_SENTINEL}",
    "mid-list": _SENTINEL,
    "end-of-list": f"zzz-{_SENTINEL}",
}


def _block_bounds(text: str | None = None) -> tuple[int, list[str]]:
    """Where the ``ExpectedImages`` block starts, and its lines verbatim.

    Derived from the template rather than written out here, so a probe below runs
    against the entries the template actually carries. This locator is scaffolding
    for the probes and is deliberately not what the gate parses with -- the
    agreement assertion in :func:`test_the_probe_block_is_the_real_block` is what
    makes a locator that reads the wrong lines visible.
    """
    lines = (text if text is not None else _template_text()).splitlines()
    starts = [
        position
        for position, line in enumerate(lines)
        if re.match(rf"^\s*{_PROPERTY}:\s*$", line)
    ]
    assert len(starts) == 1, (
        f"the probes splice a tail onto one block-style {_PROPERTY} list and found "
        f"{len(starts)} candidate lines, not 1. More than one means a second "
        "declaring site the probes would silently skip, so extend them to cover "
        "each. None means nothing matched the key line this locator looks for, "
        "which is the key alone on its line: a trailing comment on it "
        f"({_PROPERTY}:  # the list) is the likeliest cause and leaves the list "
        f"perfectly block style, while a flow sequence ({_PROPERTY}: [a, b]) or an "
        "alias moves the entries onto the key's line too. In every one of those "
        "the gate itself is unaffected, because it reads the parsed document; what "
        "needs rewriting is the splice below."
    )
    start = starts[0]
    indent = len(lines[start]) - len(lines[start].lstrip())

    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        block.append(line)
    while block and not block[-1].strip():
        block.pop()
    assert block, f"the {_PROPERTY} block at line {start + 1} reads as empty"
    assert any(line.lstrip().startswith("-") for line in block), (
        f"the {_PROPERTY} block at line {start + 1} holds no list entries, only "
        f"comments: {block}. The splices below take their indentation from an "
        "existing entry, so there is nothing to splice onto."
    )
    return start + 1, block


def _block_lines() -> list[str]:
    return _block_bounds()[1]


def _minimal_document(block: list[str]) -> str:
    """A one-resource document carrying ``block`` as its ``ExpectedImages``.

    The probes parse this rather than the whole 7,000-line template so that a
    dozen spellings cost milliseconds instead of seconds. The block itself is the
    template's own text, so what is synthetic is only the frame around it.
    """
    body = "\n".join(block)
    return (
        "Resources:\n"
        "  DockerBuildRun:\n"
        "    Type: Custom::DockerBuildRun\n"
        "    Properties:\n"
        f"      {_PROPERTY}:\n"
        f"{body}\n"
    )


def _tail_indent(block: list[str]) -> str:
    """The indentation the block's **first** entry sits at.

    First rather than any, because a nested line under an earlier entry is more
    indented and would move every splice in with it.
    """
    entries = [line for line in block if line.lstrip().startswith("-")]
    assert entries, (
        f"no list entry in {block}, so there is no indentation to splice at. "
        "_block_bounds refuses this, and a bare StopIteration from a generator "
        "would name neither the block nor the reason."
    )
    reference = entries[0]
    return reference[: len(reference) - len(reference.lstrip())]


def _splice(block: list[str], tail: list[str]) -> list[str]:
    """``tail`` appended to ``block``, with ``{i}`` and ``{s}`` filled in.

    Each case spells its own indentation through ``{i}`` rather than having one
    rule applied to all of them, because the indentation *is* the variable in
    several of these cases.
    """
    indent = _tail_indent(block)
    return block + [line.format(i=indent, s=_SENTINEL) for line in tail]


# (case id, tail lines appended to the real block, the entries they add)
_SPELLINGS: list[tuple[str, list[str], list[str]]] = [
    (
        "comment-after-a-value",
        ['{i}- "{s}"  # the one the buildspec renamed'],
        [_SENTINEL],
    ),
    (
        "comment-on-its-own-line",
        ["{i}# the entry below is the one that went unchecked", '{i}- "{s}"'],
        [_SENTINEL],
    ),
    (
        "indented-comment",
        ["{i}        # indented further than the entries", '{i}- "{s}"'],
        [_SENTINEL],
    ),
    (
        "comment-flush-left",
        ["# flush against column zero", '{i}- "{s}"'],
        [_SENTINEL],
    ),
    (
        "trailing-comment-on-the-last-line",
        ['{i}- "{s}"  # and nothing after it'],
        [_SENTINEL],
    ),
    (
        "hash-inside-a-double-quoted-scalar",
        ['{i}- "probe#sentinel-function"'],
        [_HASHED_SENTINEL],
    ),
    (
        "hash-inside-a-single-quoted-scalar",
        ["{i}- 'probe#sentinel-function'"],
        [_HASHED_SENTINEL],
    ),
    ("single-quoted-entry", ["{i}- '{s}'"], [_SENTINEL]),
    ("bare-unquoted-entry", ["{i}- {s}"], [_SENTINEL]),
    ("folded-block-scalar-entry", ["{i}- >-", "{i}    {s}"], [_SENTINEL]),
    (
        "literal-block-scalar-containing-a-hash",
        ["{i}- |-", "{i}    {s}", "{i}    # not a comment, it is content"],
        [f"{_SENTINEL}\n# not a comment, it is content"],
    ),
    (
        "blank-line-before-the-tail",
        ["", '{i}- "{s}"'],
        [_SENTINEL],
    ),
    (
        "two-entries-after-a-comment",
        ["{i}# a note", '{i}- "{s}"', '{i}- "probe-second-function"'],
        [_SENTINEL, "probe-second-function"],
    ),
    (
        "a-line-that-only-looks-like-the-end-of-the-block",
        ['{i}- "{s}"  # Resources:', '{i}- "probe-second-function"'],
        [_SENTINEL, "probe-second-function"],
    ),
]


def _splice_index(position: str, block: list[str]) -> int:
    """Where the comparison probe's sentinel goes, as an offset into the block.

    A function rather than a dict literal inside the test, so its refusal is
    reachable from a probe the way every other refusal in this module is. Against
    the real thirteen-entry block it can never fire, and a refusal nothing can
    reach is one nobody has measured.
    """
    indices = {
        "start-of-list": 0,
        "mid-list": len(block) - 1,
        "end-of-list": len(block),
    }
    middle = indices["mid-list"]
    assert 0 < middle < len(block), (
        f"the {_PROPERTY} block is {len(block)} line(s) long, so the mid-list splice "
        f"offset is {middle}, which is not strictly inside it. The three positions "
        "then stop being three distinct inputs and `mid-list` measures what one of "
        "the ends already does. Splice at a named interior entry instead."
    )
    return indices[position]


def test_the_probe_block_is_the_real_block() -> None:
    """Control: the probes' locator and the gate's parse must agree.

    Without this, a locator that reads the wrong lines would leave every probe
    below asserting against a block the template does not have -- and they would
    still pass, because each one only ever compares the parse to what the locator
    found.
    """
    block = _block_lines()
    assert _expected_images_in_text(_minimal_document(block)) == _expected_images(), (
        "the block the probes splice into does not parse to the same entries as the "
        f"template's own {_PROPERTY}, so the probes are measuring something else"
    )


@pytest.mark.parametrize(
    ("tail", "added"),
    [(tail, added) for _, tail, added in _SPELLINGS],
    ids=[case for case, _, _ in _SPELLINGS],
)
def test_the_parse_sees_every_entry_whatever_its_spelling(
    tail: list[str], added: list[str]
) -> None:
    real = _expected_images()
    assert real, "nothing to splice onto"

    spliced = _splice(_block_lines(), tail)
    parsed = _expected_images_in_text(_minimal_document(spliced))
    assert parsed == real + added, (
        f"the parse returned {parsed}, not {real + added}. An entry it cannot see is "
        "an entry the subset assertion below never checks, which is the whole of this "
        "gate's promise."
    )
    assert not set(added) & _built_images(), (
        f"the probe's injected tail {added} is a name the buildspec really builds, so "
        "the subset check below would not have flagged it. Rename the sentinel."
    )


@pytest.mark.parametrize("position", list(_POSITION_SENTINELS))
def test_an_unbuilt_entry_after_a_comment_fails_the_subset_check(
    position: str,
) -> None:
    """The issue's own measurement, against the real template text.

    A comment plus an unbuilt image after it has to reach the subset assertion.
    Spliced into the whole template rather than the one-resource frame the probes
    use, so nothing about the frame can be what makes it visible.

    What each of the three positions buys is worth being exact about, because the
    obvious summary -- "one tests the parse, three test the comparison" -- is not
    quite what is true.

    :func:`_unbuilt` is where a silently-dropped entry would next be possible after
    the parse, and the shapes a comparison realistically acquires act on the *ends*
    of a list: a slice, a sort then a slice, a ``reverse=True`` then a slice.
    ``start-of-list`` and ``end-of-list`` are what catch those, and they need the
    sentinel to land at that end in **both** of the orders such a comparison might
    truncate -- document order, and sorted order, since ``_unbuilt`` sorts before it
    subtracts. That is why each position brings its own sentinel from
    :data:`_POSITION_SENTINELS` and why both orders are asserted: with one interior
    name for all three, ``set(sorted(set(expected))[:-1]) - built`` passed every
    position, and that is the drop-the-tail shape the whole module exists to prevent.

    ``mid-list`` is not what catches an end-truncating comparison. It covers the
    parse at an interior block offset, and it keeps the exact-equality of
    ``_unbuilt`` measured there rather than only at the ends. Its own property is
    that it really is interior in both orders, which is asserted, because a
    ``mid-list`` that collapsed onto one of the ends would leave two of the three
    cases measuring the same input.

    Two spellings remain uncovered, measured and left so deliberately: dropping a
    fixed interior index, and dropping the element at ``len(expected) // 2``. No
    splice position catches either, because they need an index nothing plausible
    writes -- a comparison that discards an interior element is not a shape a slice,
    a sort or a set operation produces.
    """
    sentinel = _POSITION_SENTINELS[position]
    assert sentinel not in _built_images(), (
        f"the {position} sentinel {sentinel} is a name {_buildspec_path().name} "
        "really builds, so the subset check below could not flag it. Rename it."
    )

    lines = _template_text().splitlines()
    start, block = _block_bounds()
    indent = _tail_indent(block)
    index = _splice_index(position, block)
    mutated = (
        lines[: start + index]
        + [f"{indent}# a note about the entry below"]
        + [f'{indent}- "{sentinel}"']
        + lines[start + index :]
    )

    parsed = _expected_images_in_text("\n".join(mutated) + "\n")
    assert sentinel in parsed, (
        f"{sentinel} was spliced in {position} after a comment and the parse did "
        "not return it"
    )

    by_sort = sorted(set(parsed))
    if position == "mid-list":
        assert sentinel not in (parsed[0], parsed[-1]), (
            f"the mid-list splice landed at an end of the parse, so this case "
            f"duplicates one of the other two and nothing measures the interior: "
            f"{parsed}"
        )
        assert sentinel not in (by_sort[0], by_sort[-1]), (
            f"the mid-list sentinel sorts to an end of {by_sort}, so under a "
            "comparison that sorts before truncating this case duplicates one of "
            "the other two"
        )
    else:
        document_end = parsed[0] if position == "start-of-list" else parsed[-1]
        assert document_end == sentinel, (
            f"the {position} splice did not land where it was meant to, so a "
            "comparison discarding that end of the document order would still see "
            f"it: {parsed}"
        )
        sort_end = by_sort[0] if position == "start-of-list" else by_sort[-1]
        assert sort_end == sentinel, (
            f"{sentinel} does not sort to the {position} end of {by_sort}, so a "
            "comparison that sorts before truncating would still see it. The "
            "sentinel's name has to sort with the position it is spliced at."
        )

    assert _unbuilt(parsed, _built_images()) == [sentinel], (
        f"an unbuilt image spliced in {position} after a comment did not reach the "
        f"subset check: the parse returned {parsed}"
    )


def test_an_intrinsic_entry_is_refused_rather_than_skipped() -> None:
    """A computed tag the gate cannot resolve must fail loudly.

    Skipping it would leave that entry in exactly the state this gate exists to
    prevent -- named by the custom resource, unchecked against the build.
    """
    block = _block_lines()
    indent = _tail_indent(block)
    spliced = block + [f'{indent}- !Sub "${{SomeParameter}}-function"']

    with pytest.raises(AssertionError, match="an intrinsic such as !Sub or !Ref"):
        _expected_images_in_text(_minimal_document(spliced))


def test_a_non_list_property_is_refused_rather_than_skipped() -> None:
    document = _load_text(
        "Resources:\n"
        "  DockerBuildRun:\n"
        "    Properties:\n"
        f"      {_PROPERTY}: !Ref SomeParameter\n"
    )
    with pytest.raises(AssertionError, match="not a list"):
        _expected_images_in(document)


@pytest.mark.parametrize("conditional", ["Fn::If", "!If"])
def test_the_walk_descends_into_a_sequence(conditional: str) -> None:
    """A property inside an ``Fn::If`` sits under a *list*, not a mapping.

    Both spellings of the intrinsic put their two branches in a sequence, so a
    walk that recursed through mappings only would read neither branch's
    ``ExpectedImages`` and the subset assertion would then be comparing an empty
    list -- the vacuous pass this module's non-vacuity tests exist to stop, reached
    from the other end. ``Fn::If``-wrapped properties are ordinary in this
    repository's templates, so this is a shape the gate has to cover rather than a
    hypothetical one.
    """
    document = _load_text(
        "Resources:\n"
        "  DockerBuildRun:\n"
        "    Properties:\n"
        f"      {conditional}:\n"
        "        - SomeCondition\n"
        f"        - {_PROPERTY}:\n"
        '            - "alpha-function"\n'
        f"        - {_PROPERTY}:\n"
        '            - "beta-function"\n'
    )
    assert _expected_images_in(document) == ["alpha-function", "beta-function"], (
        f"the walk did not reach {_PROPERTY} inside a {conditional} sequence: "
        f"{_expected_images_in(document)}"
    )


# --------------------------------------------------------------------------- #
# the refusals above are reachable
#
# Every assertion in the parsing half fires on a shape the template does not
# currently have, so none of them is exercised by the live assertions -- removing
# any one of them leaves this module green -- which is how six of them were found:
# mutating the parsing half assertion by assertion, every survivor was an assertion
# in exactly that position. Each is therefore driven here against a synthetic
# document, which is the only input that can distinguish the guard from its
# absence. No count is quoted because the mutation set is not in this repository
# and the figure would be unverifiable here.
# --------------------------------------------------------------------------- #


def test_a_document_declaring_nothing_is_refused_rather_than_read_as_empty() -> None:
    """An empty parse would make the subset assertion trivially true."""
    with pytest.raises(AssertionError, match=f"no {_PROPERTY} list found"):
        _expected_images_in({})


def test_the_locator_stops_at_a_sibling_property() -> None:
    """A property beside ``ExpectedImages`` ends the block, at equal indentation.

    The real block is the last property of its resource, so nothing follows it
    there and a locator that ran one indentation level too far would behave
    identically on the template.
    """
    start, block = _block_bounds(
        "Resources:\n"
        "  DockerBuildRun:\n"
        "    Properties:\n"
        f"      {_PROPERTY}:\n"
        '        - "alpha-function"\n'
        "      ServiceToken: arn:fake\n"
    )
    assert block == ['        - "alpha-function"'], block
    assert start == 4, start


def test_a_second_declaring_site_fails_the_probe_locator() -> None:
    """Two blocks means the probes would only ever splice into one of them.

    The gate's own parse unions every site, so the right answer is to extend the
    probes rather than to let them quietly cover the first.
    """
    text = (
        "Resources:\n"
        "  One:\n"
        "    Properties:\n"
        f"      {_PROPERTY}:\n"
        '        - "alpha-function"\n'
        "  Two:\n"
        "    Properties:\n"
        f"      {_PROPERTY}:\n"
        '        - "beta-function"\n'
    )
    assert _expected_images_in_text(text) == ["alpha-function", "beta-function"], (
        "the gate's parse must union every declaring site"
    )
    with pytest.raises(AssertionError, match="candidate lines"):
        _block_bounds(text)


@pytest.mark.parametrize(
    ("case", "text", "expected"),
    [
        (
            "no-lines-under-the-key",
            f"A:\n  {_PROPERTY}:\nB: 1\n",
            "reads as empty",
        ),
        (
            "comments-only-under-the-key",
            f'A:\n  {_PROPERTY}:\n    # all commented out\n    # - "a-function"\nB: 1\n',
            "holds no list entries",
        ),
    ],
)
def test_a_block_with_nothing_to_splice_onto_is_refused(
    case: str, text: str, expected: str
) -> None:
    """Both empty shapes refuse by name, rather than raising from a generator.

    A commented-out list is what a refactor leaves behind, and before this the
    second shape reached ``_tail_indent`` and raised a bare ``StopIteration`` --
    naming neither the block nor the reason. The gate proper survives either
    (the property parses as ``None`` and is refused as "not a list"); this is the
    probe scaffolding saying so too.
    """
    with pytest.raises(AssertionError, match=expected):
        _block_bounds(text)


def test_a_block_too_short_to_have_an_interior_is_refused() -> None:
    """A one-entry block would make ``mid-list`` the same input as ``start-of-list``.

    Silently, which is the shape that matters: both cases would pass, the pair would
    read as two measurements, and the interior would be covered by neither. The real
    block holds thirteen entries, so this refusal cannot fire against the template
    and is driven synthetically, like every other refusal in this module.
    """
    with pytest.raises(AssertionError, match="not strictly inside it"):
        _splice_index("mid-list", ['        - "alpha-function"'])


def test_the_three_splice_positions_are_distinct_against_the_real_block() -> None:
    """And the live block is long enough, which is what makes the refusal inert."""
    block = _block_lines()
    offsets = {
        position: _splice_index(position, block) for position in _POSITION_SENTINELS
    }
    assert len(set(offsets.values())) == 3, offsets
    assert offsets["start-of-list"] < offsets["mid-list"] < offsets["end-of-list"], (
        offsets
    )


def _buildspec_document(*values: str) -> str:
    resources = "".join(
        f"  Project{index}:\n"
        "    Type: AWS::CodeBuild::Project\n"
        "    Properties:\n"
        "      Source:\n"
        f"        BuildSpec: {value}\n"
        for index, value in enumerate(values)
    )
    return f"Resources:\n{resources}"


def test_a_buildspec_the_repository_does_not_have_is_refused() -> None:
    document = _load_text(_buildspec_document("patterns/unified/no-such-buildspec.yml"))
    with pytest.raises(AssertionError, match="which does not exist"):
        _buildspec_path_in(document)


def test_two_distinct_buildspecs_are_refused() -> None:
    """Which one the gate would read is undetermined, so it reads neither."""
    document = _load_text(
        _buildspec_document("patterns/unified/buildspec.yml", "other/buildspec.yml")
    )
    with pytest.raises(AssertionError, match="more than one BuildSpec"):
        _buildspec_path_in(document)


def test_a_computed_buildspec_is_refused() -> None:
    document = _load_text(_buildspec_document('!Sub "${Prefix}/buildspec.yml"'))
    with pytest.raises(AssertionError, match="not a literal path"):
        _buildspec_path_in(document)


def test_a_document_with_no_buildspec_is_refused() -> None:
    with pytest.raises(AssertionError, match="no BuildSpec property found"):
        _buildspec_path_in({})


def test_an_export_the_loop_does_not_name_is_not_a_built_image() -> None:
    """The loop, not the export list, is the universe of images a build produces.

    Reading the exports instead would make the built set a superset, which only
    ever weakens the subset assertion -- and the buildspec exports exactly what it
    loops over today, so the difference is invisible against the real file.
    """
    commands = [
        'export FUNCTION_ALPHA="src/lambda/alpha_function"\n'
        'export FUNCTION_BETA="src/lambda/beta_function"',
        "for func_var in FUNCTION_ALPHA; do echo $func_var; done",
    ]
    exports = _exported_functions(commands)
    looped = _looped_functions(commands)
    assert set(exports) - set(looped), (
        f"the synthetic buildspec exports {sorted(exports)} and loops over "
        f"{sorted(looped)}, so there is no un-looped export and the assertion below "
        "would hold whichever list the implementation read"
    )

    built = _built_images_from(commands)
    assert built == frozenset({"alpha-function"}), built


# --------------------------------------------------------------------------- #
# the assertion
# --------------------------------------------------------------------------- #


def test_every_expected_image_is_actually_built() -> None:
    expected = _expected_images()
    built = _built_images()

    unbuilt = _unbuilt(expected, built)
    assert not unbuilt, (
        "DockerBuildRun's ExpectedImages names images that "
        f"{_buildspec_path().name} does not build: {unbuilt}. The custom resource "
        "polls for every expected tag, and presence polling is unbounded, so each "
        "of these waits out CloudFormation's one-hour custom-resource limit and "
        "fails the stack with 'CloudFormation did not receive a response from your "
        "Custom Resource' -- a message that names neither the image nor the build. "
        f"Images the buildspec does build: {sorted(built)}"
    )
