# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Nothing may replace a configuration profile head item and drop its head fields.

The configuration table's profile head row (``Configuration = Config#<profile>``)
carries five attributes that no single writer owns end to end. ``LatestRevision``
and ``PublishedRevision`` are maintained by ``ConfigRevisionStore`` with targeted
``ADD``/``SET`` calls; ``BdaProjectArn``, ``BdaSyncStatus`` and
``BdaLastSyncedAt`` by ``ConfigurationManager``'s BDA methods. idp_common names
them ``_PRESERVED_HEAD_FIELDS`` and states the rule in a comment above the tuple:
``put_item`` replaces the whole item, so they must be read back and re-attached on
every write or they are silently dropped.

``register_feature_hooks`` re-attached seven fields on its ``put_item`` and not one
of them was a member of that tuple, so every call deleted all five and reported
success. What that costs is not the attribute but what happens next: DynamoDB reads
an absent attribute as zero for ``ADD``, so ``next_number`` hands out revision 1
again and the next save **overwrites** the revision-1 body already in S3 under a
key that is write-once by construction. The counter itself can be recomputed from
the surviving revision index; the overwritten body cannot, and neither can
``PublishedRevision``, which nothing else records. Issue #1111.

Two things made that survive review, and there is one assertion here for each.

**The re-attach list was hand-maintained.** A list like that is a promise to
remember, and forgetting costs an attribute permanently with nothing reporting it.
:func:`test_every_read_modify_write_of_a_profile_head_preserves_the_head_fields`
finds every ``put_item`` that replaces an item the same function has just read, and
requires it to derive its preserved set from ``_PRESERVED_HEAD_FIELDS`` or to copy
the read item wholesale. A writer that uses ``update_item`` instead is not in the
universe at all, because a targeted update cannot have this failure mode.

**The module kept its own copy of the head-metadata field list, and it had drifted.**
That list is what separates head bookkeeping from the configuration body, so an
attribute missing from it is read back as though it were a config section.
:func:`test_every_head_metadata_field_list_covers_the_preserved_fields` requires
every copy of it to cover all five.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_MANAGER = "lib/idp_common_pkg/idp_common/config/configuration_manager.py"

# The row-key prefix of a configuration PROFILE head. The configuration table is
# shared with other row families keyed on the same attribute (`BdaProject#`,
# `ConfigRevIndex#`), and the five head fields belong only to this one. A site
# whose prefix cannot be determined statically is treated as in-scope, so the
# narrowing can only ever be applied where the code says so outright.
_PROFILE_KEY_PREFIX = "Config#"

# Attribute names that identify a collection literal as a copy of the head-item
# metadata field list, wherever it is declared and whatever it is called. Four of
# the six is the threshold: the shortest real copy in the tree has five members.
_METADATA_LIST_ANCHORS = frozenset(
    {"Configuration", "CreatedAt", "UpdatedAt", "IsActive", "Description", "Managed"}
)
_MIN_ANCHOR_OVERLAP = 4

# Read-modify-write replacements of a profile head that do NOT preserve the head
# fields. Each entry is one function in one file, and the reason answers for that
# function alone. Both predate #1111 and both are reported there; neither is
# reachable from the defect this module's other assertions were written for, and
# fixing either means changing a write path with its own blast radius.
#
# The ratchet is that this list may not go stale in either direction: an entry the
# discovery no longer reports as an unprotected head writer FAILS, whether because
# the site was fixed (delist it) or because it moved. So it cannot quietly
# pre-exempt whatever next occupies the path.
_KNOWN_UNPRESERVED_HEAD_WRITERS: Dict[Tuple[str, str], str] = {
    (
        "feature-platform/main-stack-extensions/lambdas/apply_feature_config_preset/index.py",
        "_write_sparse",
    ): (
        "The documented fallback taken only when the idp_common layer is absent or "
        "the preset cannot be merged over the host default; the normal path goes "
        "through ConfigurationManager.save_configuration and inherits its "
        "preservation. It re-attaches CreatedAt and IsActive, so a reinstall over an "
        "existing feature profile drops all five. Reported in #1111 and not fixed "
        "here: it writes Config#<featureId>, a profile this module's change does not "
        "touch, and correcting it is a change to the preset write path."
    ),
    (
        "src/lambda/update_configuration/index.py",
        "save_configuration_bypass_manager",
    ): (
        "Runs from the stack-deployment custom resource when a version migration is "
        "performed, i.e. across an upgrade, which is exactly when losing "
        "PublishedRevision matters. It already performs the get_item that "
        "preservation needs and copies CreatedAt, IsActive and Description from it, "
        "so the fix is small — but it is a deployment-time write path and belongs in "
        "its own change. Reported in #1111."
    ),
}

# Size of the discovered universe, pinned so a refactor that stops the walk finding
# a site fails here instead of passing vacuously. An equality rather than a floor:
# a floor catches the walk collapsing but leaves headroom for one site to slip out
# of it unnoticed.
_EXPECTED_HEAD_WRITE_SITES = 5
_MIN_METADATA_LISTS = 3


class WriteSite(NamedTuple):
    path: str
    function: str
    lineno: int
    preserves_head_fields: bool
    copies_read_item_wholesale: bool

    @property
    def key(self) -> Tuple[str, str]:
        return (self.path, self.function)

    @property
    def is_protected(self) -> bool:
        return self.preserves_head_fields or self.copies_read_item_wholesale


class MetadataList(NamedTuple):
    path: str
    lineno: int
    members: frozenset


def _tracked_python_files() -> List[str]:
    """Every tracked ``.py`` path, so the walk cannot reach build output.

    ``git ls-files`` rather than a filesystem walk for the same reason
    ``scripts/tests/exemption_discovery.py`` uses it: a sibling worktree or a
    packaged copy of the library under ``feature-platform/`` would otherwise be
    reported as a finding against this tree.
    """
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def _is_test_path(rel: str) -> bool:
    name = Path(rel).name
    return (
        name.startswith("test_")
        or name == "conftest.py"
        or "/tests/" in rel
        or rel.startswith("tests/")
    )


def _preserved_head_fields() -> Tuple[str, ...]:
    """``_PRESERVED_HEAD_FIELDS`` read out of the source, not imported.

    Reading the source keeps this module free of an ``idp_common`` import (the
    scripts suite runs without the library installed) and makes a rename of the
    constant fail here rather than silently emptying the assertion.
    """
    tree = ast.parse((REPO_ROOT / _MANAGER).read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if "_PRESERVED_HEAD_FIELDS" not in targets:
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
            break
        return tuple(
            e.value
            for e in node.value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        )
    raise AssertionError(
        f"_PRESERVED_HEAD_FIELDS is no longer a module-level string collection in "
        f"{_MANAGER}. It is the subject of every assertion in this file; if it moved, "
        f"point _MANAGER at its new home rather than deleting these checks."
    )


def _attribute_calls(node: ast.AST):
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
            yield inner.func.attr, inner


def _names_the_configuration_key(node: ast.AST) -> bool:
    """True when the tree holds a dict whose key is the literal ``Configuration``.

    The configuration table's partition key, asserted structurally rather than by
    searching the source text: ``Configuration`` is an ordinary English word and
    appears in docstrings and parameter descriptions all over the tree, which put a
    ``jobId``-keyed discovery-tracking table into an earlier version of this walk.
    """
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Dict):
            continue
        for key in inner.keys:
            if isinstance(key, ast.Constant) and key.value == "Configuration":
                return True
    return False


def _configuration_table_operations(tree: ast.AST) -> Set[str]:
    """The DynamoDB operations this module performs against a ``Configuration`` key.

    Judged per enclosing function rather than per call, because the key is almost
    never spelled at the call itself: ``put_item(Item=compressed_item)`` names no key
    at all, and the one that identifies the table is on the ``get_item`` a few lines
    above. Function scope is the smallest unit where both are visible.

    This is what separates a module that rewrites a profile head from one that reads
    a profile and writes somewhere else — the pipeline-hooks dispatcher and the test
    runner both read a profile and then write ``PK``-keyed tracking rows, so their
    writes do not qualify.
    """
    operations: Set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _names_the_configuration_key(fn):
            continue
        for name, _ in _attribute_calls(fn):
            if name in ("get_item", "put_item", "update_item"):
                operations.add(name)
    return operations


def _string_prefix(node: ast.AST) -> Optional[str]:
    """The literal leading text of a string or f-string expression, if there is one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
            return None
    return None


def _local_string_prefixes(fn: ast.AST) -> Dict[str, str]:
    """Local name -> literal prefix of the string it is assigned, one level deep."""
    prefixes: Dict[str, str] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        else:
            continue
        if value is None:
            continue
        prefix = _string_prefix(value)
        if prefix is None:
            continue
        for target in targets:
            prefixes[target] = prefix
    return prefixes


def _row_key_prefix(call: ast.Call, fn: ast.AST) -> Optional[str]:
    """The ``Configuration`` value's literal prefix for a ``put_item`` call.

    ``None`` when it cannot be determined, which keeps the site in scope.
    """
    item = None
    for kw in call.keywords:
        if kw.arg == "Item":
            item = kw.value
    if item is None:
        return None
    locals_ = _local_string_prefixes(fn)
    candidates: List[ast.AST] = [item]
    if isinstance(item, ast.Name):
        candidates.extend(_assignments_to(item.id, fn))
    for candidate in candidates:
        if not isinstance(candidate, ast.Dict):
            continue
        for key, value in zip(candidate.keys, candidate.values):
            if not (isinstance(key, ast.Constant) and key.value == "Configuration"):
                continue
            direct = _string_prefix(value)
            if direct is not None:
                return direct
            if isinstance(value, ast.Name):
                return locals_.get(value.id)
            return None
    return None


def discover_head_write_sites() -> List[WriteSite]:
    """Every ``put_item`` that replaces a configuration item the caller just read.

    A read followed by a whole-item write is the shape that loses attributes; a
    function that writes a freshly built item (the active-version pointer, a
    benchmark upload) has no read whose state it can silently discard, and a
    function that uses ``update_item`` cannot drop an attribute it does not name.
    Neither is in scope, and both are excluded by what the code does rather than by
    a list here.
    """
    sites: List[WriteSite] = []
    for rel in _tracked_python_files():
        if _is_test_path(rel):
            continue
        try:
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        if "put_item" not in source or "Configuration" not in source:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = {name for name, _ in _attribute_calls(fn)}
            if not {"put_item", "get_item"} <= called:
                continue
            if not _names_the_configuration_key(fn):
                continue
            body = ast.get_source_segment(source, fn) or ""
            preserves = "_PRESERVED_HEAD_FIELDS" in body
            for name, call in _attribute_calls(fn):
                if name != "put_item":
                    continue
                prefix = _row_key_prefix(call, fn)
                if prefix is not None and not prefix.startswith(_PROFILE_KEY_PREFIX):
                    # A different row family in the same table, named outright by
                    # the code (e.g. `f"BdaProject#{version}"`).
                    continue
                wholesale = _writes_a_wholesale_copy(call, fn)
                sites.append(WriteSite(rel, fn.name, call.lineno, preserves, wholesale))
    return sites


def _assignments_to(name: str, fn: ast.AST):
    """Every value assigned to a local, through ``x = ...`` and ``x: T = ...``.

    Both spellings, because reading only ``ast.Assign`` makes this gate's verdict
    depend on whether the author wrote a type annotation. The defect this module
    exists for used the annotated form, so the annotation was the only thing
    stopping :func:`_is_wholesale_source` from clearing it — see
    :func:`test_a_filtered_spread_is_not_a_wholesale_copy`.
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                yield node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                if node.value is not None:
                    yield node.value


def _names_derived_from_a_read(fn: ast.AST) -> Set[str]:
    """Locals whose value came, directly or transitively, from a ``get_item``.

    Computed to a fixpoint, because the read is rarely one statement: the shape in
    the tree is ``response = table.get_item(...)`` and then ``item =
    response["Item"]``, so the name the writer copies is two steps from the call.
    """
    read: Set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
                value = node.value
            else:
                continue
            if value is None or not targets:
                continue
            derived = any(
                (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "get_item"
                )
                or (isinstance(sub, ast.Name) and sub.id in read)
                for sub in ast.walk(value)
            )
            if derived:
                for target in targets:
                    if target not in read:
                        read.add(target)
                        changed = True
    return read


def _is_wholesale_source(node: ast.AST, read_names: Set[str]) -> bool:
    """True when this expression evaluates to every attribute of the item just read.

    Two conditions, and both are load-bearing.

    The **shape** must actually carry everything: a bare name, ``dict(x)``,
    ``x.copy()`` or ``copy.deepcopy(x)``, and nothing else. Reading any ``**`` as
    wholesale is wrong, because ``**{k: v for k, v in payload.items() if k not in
    METADATA}`` is a spread of a FILTERED comprehension and carries a subset — and
    that is exactly the defective writer, so the loose reading would clear the one
    case this gate exists for.

    The **operand** must be the dict that was read. Spreading a whole dict that is
    not the stored item preserves nothing about the stored item:
    ``apply_feature_config_preset._write_sparse`` ends its item with ``**config``,
    the preset body it was handed, while the row it read is bound to ``existing``
    and contributes two fields. Judged on shape alone that writer reads as safe,
    which would drop a genuinely unprotected head write out of this gate's sight.
    """
    if isinstance(node, ast.Name):
        return node.id in read_names
    if isinstance(node, ast.Call):
        # dict(existing) — but not dict(**filtered) or dict(a=1)
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "dict"
            and len(node.args) == 1
            and not node.keywords
            and _is_wholesale_source(node.args[0], read_names)
        ):
            return True
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "copy" and not node.args:
                return _is_wholesale_source(node.func.value, read_names)
            if node.func.attr == "deepcopy" and len(node.args) == 1:
                return _is_wholesale_source(node.args[0], read_names)
    return False


def _writes_a_wholesale_copy(call: ast.Call, fn: ast.AST) -> bool:
    """True when the written item carries every attribute of a dict it copied.

    ``new = dict(existing)`` then mutate then ``put_item(Item=new)`` cannot drop a
    head field, and neither can ``{**existing, "Configuration": ...}``. This is a
    legitimate second way to be correct and the legacy ``Default`` ->
    ``Config#default`` migration uses it, so the gate has to recognise it — but
    only where the thing being spread really is a whole dict. See
    :func:`_is_wholesale_source` for why that distinction decides whether this gate
    can see its own case.
    """
    item = None
    for kw in call.keywords:
        if kw.arg == "Item":
            item = kw.value
    if item is None:
        return False
    read_names = _names_derived_from_a_read(fn)

    def _dict_spreads_wholesale(node: ast.AST) -> bool:
        if not isinstance(node, ast.Dict):
            return False
        return any(
            key is None and _is_wholesale_source(value, read_names)
            for key, value in zip(node.keys, node.values)
        )

    if _dict_spreads_wholesale(item):
        return True
    # `put_item(Item=existing.copy())` / `Item=dict(existing)` — the copy inlined at
    # the call rather than bound to a local first.
    if not isinstance(item, ast.Name) and _is_wholesale_source(item, read_names):
        return True
    if not isinstance(item, ast.Name):
        return False
    for value in _assignments_to(item.id, fn):
        if _dict_spreads_wholesale(value) or _is_wholesale_source(value, read_names):
            return True
    return False


def discover_metadata_lists() -> List[MetadataList]:
    """Every copy of the head-item metadata field list in a module that rewrites a head.

    Discovered by its CONTENT rather than by the name it is bound to, because the
    names disagree: in scope today are ``_DYNAMODB_METADATA_FIELDS`` in the
    configuration manager, which is the canonical one, and two separate
    ``_CONFIG_METADATA_FIELDS`` in the feature-platform Lambdas, which are copies of
    it. A name-based walk would have to know all three spellings in advance.

    Collections of the same attribute names exist elsewhere in the tree and are
    **not** in this universe — the pipeline-hooks dispatcher and the test runner
    read a profile and write ``PK``-keyed rows, the benchmark harness builds its
    item from scratch, and ``finetuning_deployment_handler`` describes pricing rows.
    Several of those omit the revision counters harmlessly, which is why the scope
    conditions below are worth stating precisely rather than widening.

    Scoped to modules that both READ a ``Configuration``-keyed row and WRITE one, and
    that name the ``Config#`` profile prefix. All three conditions come from the code
    rather than from a list here, and each excludes a module where omitting a head
    field is harmless:

    * A module that only reads — the pipeline-hooks dispatcher, the test runner —
      writes ``PK``-keyed tracking rows, so a counter it mis-classifies as config
      body is never written back anywhere.
    * A module that only writes, building its item from scratch — the benchmark
      harness's synthetic ``Config#bench-*`` upload — has no read whose attributes
      it can discard.
    * A module describing a different row family — the pricing copy in
      ``finetuning_deployment_handler``, which names no ``Config#`` prefix — is
      about ``DefaultPricing``/``CustomPricing`` rows, which carry no head fields.
    """
    found: List[MetadataList] = []
    for rel in _tracked_python_files():
        if _is_test_path(rel):
            continue
        try:
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        if _PROFILE_KEY_PREFIX not in source:
            continue
        operations = _configuration_table_operations(tree)
        if "get_item" not in operations or not operations & {"put_item", "update_item"}:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Set, ast.Tuple, ast.List)):
                continue
            members = frozenset(
                e.value
                for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )
            if len(members & _METADATA_LIST_ANCHORS) < _MIN_ANCHOR_OVERLAP:
                continue
            found.append(MetadataList(rel, node.lineno, members))
    return found


# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #


def test_the_preserved_head_fields_tuple_is_still_the_five_it_documents():
    """Anchors every other assertion, and fails loudly if the tuple is emptied."""
    fields = _preserved_head_fields()
    assert set(fields) == {
        "BdaProjectArn",
        "BdaSyncStatus",
        "BdaLastSyncedAt",
        "LatestRevision",
        "PublishedRevision",
    }, (
        f"_PRESERVED_HEAD_FIELDS is now {fields}. A field ADDED here is fine — update "
        f"this literal — but a field removed means some writer has stopped needing to "
        f"preserve it, which is a claim to check rather than to accept."
    )


def test_the_head_write_universe_is_the_pinned_size():
    """Count pinning, in both directions, because a floor leaves room for an escapee.

    A floor catches the walk collapsing to nothing but not one site slipping out of
    it: with five found and a floor of four there was a full site of headroom, and a
    writer whose read moves behind a helper leaves this function's scope entirely —
    ``finetuning_deployment_handler._write_pricing_config`` is a live instance of
    that shape, reading in one function and writing in another. It is out of scope
    on row-family grounds anyway (it writes pricing rows), but it is also
    structurally invisible here, so the walk's reach is narrower than it looks.

    Pinning the exact number turns any change into a deliberate edit: a new
    read-modify-write site of a profile head is a thing to look at, and so is one
    disappearing.
    """
    sites = discover_head_write_sites()
    detail = "\n".join(f"  {s.path}:{s.lineno} in {s.function}()" for s in sites)
    assert len(sites) == _EXPECTED_HEAD_WRITE_SITES, (
        f"Discovered {len(sites)} read-modify-write profile-head write site(s), pinned "
        f"at {_EXPECTED_HEAD_WRITE_SITES}:\n{detail}\n\n"
        f"If you ADDED one, it must satisfy the assertion below on its own merits — "
        f"then raise the pin. If one VANISHED, check it was fixed or removed rather "
        f"than merely moved out of this walk's reach: the walk keys on one function "
        f"calling both get_item and put_item, so splitting a writer across two "
        f"functions takes it out of scope without fixing anything."
    )


def test_every_read_modify_write_of_a_profile_head_preserves_the_head_fields():
    """The assertion the register_feature_hooks defect would have failed.

    A whole-item ``put_item`` over an item the same function read must either derive
    its preserved set from ``_PRESERVED_HEAD_FIELDS`` or copy the read item
    wholesale. Re-listing the fields by hand is what failed, so naming the constant
    is what counts.
    """
    unprotected = [s for s in discover_head_write_sites() if not s.is_protected]
    unregistered = [
        s for s in unprotected if s.key not in _KNOWN_UNPRESERVED_HEAD_WRITERS
    ]
    assert not unregistered, "\n".join(
        [
            "These sites replace a configuration profile head item they just read, "
            "without preserving the head fields that other writers own:",
            *(f"  {s.path}:{s.lineno} in {s.function}()" for s in unregistered),
            "",
            "Prefer update_item with an explicit expression: an attribute it does not "
            "name survives, so a head field added later needs no edit. If it must stay "
            "a put_item, re-read the fields with a ProjectionExpression over "
            "_PRESERVED_HEAD_FIELDS as ConfigurationManager._write_record does — derive "
            "the set, never restate it.",
        ]
    )


def test_no_registered_head_writer_exemption_has_gone_stale():
    """Non-vacuity and staleness, per entry.

    An entry that shields nothing is dead: either the site was fixed, in which case
    delist it, or it moved, in which case the gate is no longer watching it. Both
    must fail rather than pass quietly, because a dead entry pre-exempts whatever
    next occupies that path. Each entry is therefore required to still match a
    discovered, still-unprotected write site — it must be still needed.
    """
    unprotected = {s.key for s in discover_head_write_sites() if not s.is_protected}
    dead = sorted(k for k in _KNOWN_UNPRESERVED_HEAD_WRITERS if k not in unprotected)
    assert not dead, (
        f"These registered exemptions no longer shield an unprotected head write: "
        f"{dead}. If the site now preserves the head fields, delete its entry from "
        f"_KNOWN_UNPRESERVED_HEAD_WRITERS. If it was renamed or moved, update the key "
        f"— leaving it keyed to a path that no longer writes the head means the next "
        f"writer to occupy that function name is exempt before it is written."
    )


@pytest.mark.parametrize(
    "entry", sorted(_KNOWN_UNPRESERVED_HEAD_WRITERS), ids=lambda e: f"{e[0]}::{e[1]}"
)
def test_each_registered_head_writer_exemption_states_a_reason(entry):
    """One entry, one member's worth of reason — asserted, not trusted."""
    path, function = entry
    reason = _KNOWN_UNPRESERVED_HEAD_WRITERS[entry]
    assert (REPO_ROOT / path).is_file(), f"{path} does not exist"
    assert len(reason.split()) >= 25, (
        f"The reason for {path}::{function} is too short to answer for this site "
        f"specifically. A reason that would read the same for any member is the defect "
        f"this registry exists to stop."
    )
    assert "#1111" in reason, (
        f"The reason for {path}::{function} must cite the issue tracking it, so a "
        f"reader can tell a known carve-out from an unexamined one."
    )


def _wholesale_verdict(source: str) -> bool:
    """Run the resolver over one synthetic writer and return its verdict."""
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    call = next(c for name, c in _attribute_calls(fn) if name == "put_item")
    return _writes_a_wholesale_copy(call, fn)


#: Writers that drop head fields, each of which the gate must REPORT. Every one is
#: the defective shape; what differs is only how it is spelled, and none of those
#: differences may change the verdict.
_DEFECTIVE_SPELLINGS = {
    "annotated assignment": """
def _register(table, config_key, item, payload, timestamp):
    resp = table.get_item(Key={"Configuration": config_key})
    new_item: Dict[str, Any] = {
        "Configuration": config_key,
        "CreatedAt": item.get("CreatedAt", timestamp),
        **{k: v for k, v in payload.items() if k not in _CONFIG_METADATA_FIELDS},
    }
    table.put_item(Item=new_item)
""",
    "plain assignment": """
def _register(table, config_key, item, payload, timestamp):
    resp = table.get_item(Key={"Configuration": config_key})
    new_item = {
        "Configuration": config_key,
        **{k: v for k, v in payload.items() if k not in _CONFIG_METADATA_FIELDS},
    }
    table.put_item(Item=new_item)
""",
    "inlined at the call": """
def _register(table, config_key, payload):
    resp = table.get_item(Key={"Configuration": config_key})
    table.put_item(Item={
        "Configuration": config_key,
        **{k: v for k, v in payload.items() if k not in _CONFIG_METADATA_FIELDS},
    })
""",
    "spread of a whole dict that is not the row read": """
def _write_sparse(table, config_key, config, timestamp):
    existing = (table.get_item(Key={"Configuration": config_key})).get("Item") or {}
    item: Dict[str, Any] = {
        "Configuration": config_key,
        "CreatedAt": existing.get("CreatedAt", timestamp),
        **config,
    }
    table.put_item(Item=item)
""",
}

#: Writers that genuinely carry every attribute forward, each of which the gate must
#: stay quiet about — the legacy Default -> Config#default migration is one of them,
#: so a false positive here would fail the gate on correct code.
_WHOLESALE_SPELLINGS = {
    "dict(existing)": """
def migrate(table):
    existing = table.get_item(Key={"Configuration": "Default"})["Item"]
    new = dict(existing)
    table.put_item(Item=new)
""",
    "spread of the name that was read": """
def migrate(table):
    existing = table.get_item(Key={"Configuration": "Default"})["Item"]
    table.put_item(Item={**existing, "Configuration": "Config#default"})
""",
    "existing.copy()": """
def migrate(table):
    existing = table.get_item(Key={"Configuration": "Default"})["Item"]
    table.put_item(Item=existing.copy())
""",
    "annotated dict(existing)": """
def migrate(table):
    existing = table.get_item(Key={"Configuration": "Default"})["Item"]
    new: Dict[str, Any] = dict(existing)
    table.put_item(Item=new)
""",
    "two-step read then dict()": """
def migrate(table):
    response = table.get_item(Key={"Configuration": "Default"})
    default_item = response["Item"]
    new_default_item = dict(default_item)
    new_default_item["Configuration"] = "Config#default"
    table.put_item(Item=new_default_item)
""",
}


@pytest.mark.parametrize("label", sorted(_DEFECTIVE_SPELLINGS))
def test_a_defective_writer_is_reported_however_it_is_spelled(label):
    """The gate must not depend on an incidental property of the source.

    Every entry here is the same defect. Two of them would have cleared an earlier
    version of this resolver: it read **any** ``**`` spread as carrying everything
    forward, so a spread of a *filtered* dict comprehension — which is precisely the
    defective writer — looked safe. What saved it was that the historical instance
    happened to use an annotated assignment, and the resolver walked only
    ``ast.Assign``: dropping the annotation or inlining the dict passed the gate with
    the bug intact. A gate whose protection rests on a type annotation is not a gate.
    """
    assert not _wholesale_verdict(_DEFECTIVE_SPELLINGS[label]), (
        f"The {label!r} writer was judged a wholesale copy, so the gate would not "
        f"report it. It spreads a filtered subset, or a dict other than the row it "
        f"read, and drops every head field it does not name."
    )


@pytest.mark.parametrize("label", sorted(_WHOLESALE_SPELLINGS))
def test_a_genuine_wholesale_copy_is_not_reported(label):
    """The other direction: correct code must not fail the gate.

    ``dict(existing)`` and ``{**existing, ...}`` cannot drop an attribute, and the
    legacy ``Default`` -> ``Config#default`` migration is written that way, so
    tightening the resolver until it reported those would make the gate unusable.
    """
    assert _wholesale_verdict(_WHOLESALE_SPELLINGS[label]), (
        f"The {label!r} writer carries every attribute of the row it read, but the "
        f"resolver did not recognise it — this is a false positive that would fail "
        f"the gate on correct code."
    )


def test_a_spread_is_only_wholesale_when_its_operand_is_the_row_that_was_read():
    """Shape alone is not enough, and this is the case that shows why.

    ``{**config, ...}`` spreads a whole dict, so a resolver checking only the shape
    calls it wholesale — but ``config`` is the preset body the function was handed,
    not the stored row, and preserving all of *it* preserves nothing of the item
    being replaced. This is `_write_sparse`'s real shape, and reading it as safe
    dropped one of the two genuinely unprotected head writers out of the gate's
    sight entirely, which also silently emptied its registered exemption.
    """
    same_shape_but_reads_the_row = """
def writer(table, config_key):
    existing = (table.get_item(Key={"Configuration": config_key})).get("Item") or {}
    table.put_item(Item={**existing, "UpdatedAt": "now"})
"""
    same_shape_but_spreads_a_parameter = """
def writer(table, config_key, config):
    existing = (table.get_item(Key={"Configuration": config_key})).get("Item") or {}
    table.put_item(Item={**config, "UpdatedAt": "now"})
"""
    assert _wholesale_verdict(same_shape_but_reads_the_row)
    assert not _wholesale_verdict(same_shape_but_spreads_a_parameter)


def test_the_metadata_list_universe_is_not_empty():
    """Non-vacuity for the second assertion."""
    lists = discover_metadata_lists()
    assert len(lists) >= _MIN_METADATA_LISTS, (
        f"Only {len(lists)} head-metadata field list(s) discovered, expected at least "
        f"{_MIN_METADATA_LISTS}. Discovery is by content ({_MIN_ANCHOR_OVERLAP}+ of "
        f"{sorted(_METADATA_LIST_ANCHORS)}) in a module naming {_PROFILE_KEY_PREFIX!r}; "
        f"if the copies were consolidated that is good news, but lower this floor "
        f"deliberately rather than as a side effect."
    )


def test_every_head_metadata_field_list_covers_the_preserved_fields():
    """A metadata list missing a head field reads that field back as config body.

    This is the root of the two revision counters being lost on legacy inline rows:
    they were absent from ``register_feature_hooks._CONFIG_METADATA_FIELDS``, so
    ``_decompress`` returned them inside the configuration body and the write put
    them back as config sections. Every copy of that list must name all five.
    """
    preserved: Set[str] = set(_preserved_head_fields())
    short = [
        (m, sorted(preserved - m.members))
        for m in discover_metadata_lists()
        if not preserved <= m.members
    ]
    assert not short, "\n".join(
        [
            "These copies of the configuration head's metadata field list omit head "
            "fields that other writers own, so the module will read them back as part "
            "of the configuration body:",
            *(f"  {m.path}:{m.lineno} is missing {missing}" for m, missing in short),
        ]
    )
