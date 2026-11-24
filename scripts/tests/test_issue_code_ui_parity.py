# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every processing issue code the backend raises on a section must have a UI label, and only those.

A ``ProcessingIssue`` carries a ``code`` (``extraction_sparse``,
``assessment_incomplete``, ...). An issue raised on a section, with ``section_id=``,
is listed in the Processing issues popover of the document page's Document Sections
table, its code above its message. Each such code is a react-intl message in
``src/ui/src/components/common/processing-issues-utils.ts`` (``issueCodeMessages``)
whose English is the code itself, so English shows the stored code and a translation
can give a readable label. A code the map does not know is shown as it is.

That fallback is what makes a gap quiet. A code the backend starts raising on a
section renders in English exactly as a mapped one does, so no English test and no
screenshot notices that under another language it alone stays the raw code. In the
other direction, a label for a code no section carries is dead: translators pay for
it and nothing ever shows it.

An issue raised without ``section_id=`` is recorded on the document itself, as
ingest's warnings are. The Document Details panel lists the document's own issues by
their message alone, so a code raised only there is never shown and has no label.
Those call sites are pinned with their count in ``DOCUMENT_LEVEL_CODE_SITES``, so a
new one turns :func:`test_no_new_document_level_site_appears` red rather than
quietly leaving the set the UI is checked against.

The backend keeps no single list of its codes: each one is written at a
``ProcessingIssue(...)`` call under ``lib/idp_common_pkg/idp_common`` or
``src/lambda``, as one of

* a string literal;
* a module or class constant, ``NAME`` or ``self.NAME``;
* a constant imported from another ``idp_common`` module;
* a local variable, read as every string assigned to it in the enclosing function,
  taking both branches of a conditional expression and never its condition;
* ``diagnosis.code`` in ``idp_common.document_failure``, read as that module's
  ``*_CODE`` constants. ``idp_common.extraction.failure`` passes
  ``extraction_failed`` through it too, a code extraction also raises directly.

So this test derives the codes from the section-level call sites with ``ast``, reads
the UI map from source, and fails in **both** directions. A call site none of those rules
resolves is pinned with its count in ``UNRESOLVED_CODE_SITES``, so a new one turns
:func:`test_no_new_unresolvable_code_site_appears` red rather than silently
narrowing the set every other assertion here is about. Read from **source** rather
than imported: that keeps the gate runnable without the package installed, and the
assertion is about what is committed.
"""

from __future__ import annotations

import ast
import functools
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The UI's label map, one entry per code.
UI_MODULE = REPO_ROOT / "src/ui/src/components/common/processing-issues-utils.ts"

#: Where the backend raises processing issues.
BACKEND_ROOTS = (
    REPO_ROOT / "lib/idp_common_pkg/idp_common",
    REPO_ROOT / "src/lambda",
)

#: The directory ``from idp_common.<module> import NAME`` resolves under.
IDP_COMMON_PACKAGE_ROOT = REPO_ROOT / "lib/idp_common_pkg"

#: The module whose ``record_section_failure`` raises ``code=diagnosis.code``.
DOCUMENT_FAILURE_MODULE = (
    REPO_ROOT / "lib/idp_common_pkg/idp_common/document_failure.py"
)

#: ``ProcessingIssue(...)`` call sites whose ``code`` no rule above resolves, as
#: ``(path relative to the repo, the code expression) -> (count, why it adds no
#: code)``. Keyed on the expression's source text rather than a line number, so
#: moving code does not churn the pin.
UNRESOLVED_CODE_SITES = {
    ("lib/idp_common_pkg/idp_common/models.py", "iss.get('code', '')"): (
        1,
        "deserialize_issue_list rebuilds issues from a stored item, so each code it "
        "carries was raised at another call site this test reads; it declares none of "
        "its own.",
    ),
}

#: ``ProcessingIssue(...)`` call sites without a ``section_id=``, whose issue is
#: recorded on the document rather than a section, as ``(path relative to the repo,
#: the code expression) -> (count, what it records)``. Their codes join neither side
#: of the checks, because the Document Details panel shows a document's own issues by
#: their message alone. Keyed as ``UNRESOLVED_CODE_SITES`` is.
DOCUMENT_LEVEL_CODE_SITES = {
    ("lib/idp_common_pkg/idp_common/models.py", "'ingest_attributes_dropped'"): (
        1,
        "attributes_dropped_issue, the warning that intake dropped caller-supplied "
        "document attributes; its callers put it in a Document's processing_issues, "
        "never a section's.",
    ),
    ("src/lambda/queue_sender/index.py", "ISSUE_PREFIX_MAPPING_LOOKUP_FAILED"): (
        1,
        "_record_lookup_failure, the warning that the configuration prefix mappings "
        "could not be read at ingest; it appends the issue to "
        "document.processing_issues.",
    ),
}


def _backend_files() -> list[Path]:
    """The backend's Python modules, its tests left out (they raise made-up codes)."""
    files: list[Path] = []
    for root in BACKEND_ROOTS:
        assert root.is_dir(), (
            f"{root.relative_to(REPO_ROOT)} has moved; update BACKEND_ROOTS in this test"
        )
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(root)
            if (
                "tests" in relative.parts
                or path.name.startswith("test_")
                or path.name == "conftest.py"
            ):
                continue
            files.append(path)
    return files


def _string_assignments(body: list[ast.stmt]) -> dict[str, str]:
    """``NAME = "literal"`` among these statements (a module's or a class's body)."""
    bindings: dict[str, str] = {}
    for node in body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value.value
    return bindings


def _class_constants(tree: ast.Module) -> dict[str, str]:
    """String constants of every class body in the module, for ``self.NAME``."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bindings.update(_string_assignments(node.body))
    return bindings


@functools.lru_cache(maxsize=None)
def _module_constants(path: Path) -> dict[str, str]:
    return _string_assignments(ast.parse(path.read_text(encoding="utf-8")).body)


def _imported_constants(tree: ast.Module) -> dict[str, str]:
    """String constants imported by name from another ``idp_common`` module.

    Imports anywhere in the module count, function-level ones included, because that
    is where ``extraction/service.py`` takes ``EXTRACTION_FAILED_CODE``.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        if node.module.split(".")[0] != "idp_common":
            continue
        base = IDP_COMMON_PACKAGE_ROOT.joinpath(*node.module.split("."))
        source = next(
            (p for p in (base.with_suffix(".py"), base / "__init__.py") if p.is_file()),
            None,
        )
        if source is None:
            continue
        declared = _module_constants(source)
        for alias in node.names:
            if alias.name in declared:
                bindings[alias.asname or alias.name] = declared[alias.name]
    return bindings


@functools.lru_cache(maxsize=None)
def _document_failure_codes() -> frozenset[str]:
    assert DOCUMENT_FAILURE_MODULE.is_file(), (
        f"{DOCUMENT_FAILURE_MODULE.relative_to(REPO_ROOT)} has moved; update "
        "DOCUMENT_FAILURE_MODULE in this test"
    )
    codes = frozenset(
        value
        for name, value in _module_constants(DOCUMENT_FAILURE_MODULE).items()
        if name.endswith("_CODE")
    )
    assert codes, (
        f"{DOCUMENT_FAILURE_MODULE.relative_to(REPO_ROOT)} declares no `*_CODE` "
        "constant, so `code=diagnosis.code` resolves to nothing. If its codes moved, "
        "point this test at their new home rather than deleting the rule."
    )
    return codes


def _assigned_strings(expr: ast.expr, constants: dict[str, str]) -> set[str] | None:
    """The strings an assigned value can be, or None when any part of it is not a string.

    Both branches of a conditional expression count, its condition does not: the
    classification site's ``"classification_page_no_content" if ... else
    "classification_failed"`` has a condition that reads
    ``metadata.get("page_empty", True)``, and ``page_empty`` is not a code.
    """
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) else None
    if isinstance(expr, ast.IfExp):
        body = _assigned_strings(expr.body, constants)
        orelse = _assigned_strings(expr.orelse, constants)
        return None if body is None or orelse is None else body | orelse
    if isinstance(expr, ast.Name) and expr.id in constants:
        return {constants[expr.id]}
    return None


def _local_strings(
    function: ast.AST | None, name: str, constants: dict[str, str]
) -> set[str] | None:
    """Every string assigned to ``name`` in ``function``, or None if any is not one."""
    if function is None:
        return None
    values: set[str] = set()
    assigned = False
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        assigned = True
        strings = _assigned_strings(node.value, constants)
        if strings is None:
            return None
        values |= strings
    return values if assigned else None


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.AST | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


@functools.lru_cache(maxsize=None)
def _derive_backend() -> tuple[
    dict[str, tuple[str, ...]], dict[tuple[str, str], int], dict[tuple[str, str], int]
]:
    """``(section code -> its call sites, unresolved sites, document-level sites)``.

    Both site maps are ``(file, code expression) -> count``.
    """
    sites: dict[str, list[str]] = {}
    unresolved: dict[tuple[str, str], int] = {}
    document_level: dict[tuple[str, str], int] = {}
    calls = 0
    for path in _backend_files():
        source = path.read_text(encoding="utf-8")
        if "ProcessingIssue" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        relative = str(path.relative_to(REPO_ROOT))
        module_constants = {
            **_imported_constants(tree),
            **_string_assignments(tree.body),
        }
        class_constants = _class_constants(tree)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "ProcessingIssue":
                continue
            calls += 1
            expr = next((k.value for k in node.keywords if k.arg == "code"), None)
            key = (
                relative,
                ast.unparse(expr) if expr is not None else "<no code argument>",
            )
            if not any(k.arg == "section_id" for k in node.keywords):
                document_level[key] = document_level.get(key, 0) + 1
                continue
            codes: set[str] | None = None
            if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                codes = {expr.value}
            elif isinstance(expr, ast.Name) and expr.id in module_constants:
                codes = {module_constants[expr.id]}
            elif isinstance(expr, ast.Name):
                codes = _local_strings(
                    _enclosing_function(node, parents), expr.id, module_constants
                )
            elif (
                isinstance(expr, ast.Attribute)
                and isinstance(expr.value, ast.Name)
                and expr.value.id == "diagnosis"
                and expr.attr == "code"
                and path == DOCUMENT_FAILURE_MODULE
            ):
                codes = set(_document_failure_codes())
            elif isinstance(expr, ast.Attribute) and expr.attr in class_constants:
                codes = {class_constants[expr.attr]}
            if not codes:
                unresolved[key] = unresolved.get(key, 0) + 1
                continue
            for code in codes:
                sites.setdefault(code, []).append(f"{relative}:{node.lineno}")
    assert calls, (
        "found no ProcessingIssue(...) call under "
        + ", ".join(str(r.relative_to(REPO_ROOT)) for r in BACKEND_ROOTS)
        + ". If the backend renamed the class or moved its callers, point this test "
        "at them rather than deleting it."
    )
    return (
        {code: tuple(found) for code, found in sites.items()},
        unresolved,
        document_level,
    )


def _section_codes() -> dict[str, tuple[str, ...]]:
    return _derive_backend()[0]


@functools.lru_cache(maxsize=None)
def _ui_entries() -> dict[str, str | None]:
    """Each entry of ``const issueCodeMessages = defineMessages<...>({ ... })``: code -> its English."""
    assert UI_MODULE.is_file(), (
        f"{UI_MODULE.relative_to(REPO_ROOT)} has moved; update UI_MODULE in this test"
    )
    source = UI_MODULE.read_text(encoding="utf-8")
    match = re.search(
        r"^const issueCodeMessages = defineMessages<[^\n]*>\(\{\n(.*?)^\}\);",
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert match, (
        f"{UI_MODULE.relative_to(REPO_ROOT)} no longer declares "
        "`const issueCodeMessages = defineMessages<...>({` on one line. That map is "
        "what gives each processing issue code a translatable label; if it was "
        "restructured, update this test to read the new shape rather than removing it."
    )
    # One `  code: {` per entry at the map's own indentation, up to its `  },`; the
    # `id` / `defaultMessage` / `description` lines sit deeper.
    entries: dict[str, str | None] = {}
    for code, body in re.findall(
        r"^  ([a-z][a-z0-9_]*): \{\n(.*?)^  \},$",
        match.group(1),
        re.DOTALL | re.MULTILINE,
    ):
        english = re.search(r"^\s+defaultMessage:\s*'([^'\n]*)',$", body, re.MULTILINE)
        entries[code] = english.group(1) if english else None
    return entries


def _camel_case(code: str) -> str:
    head, *rest = code.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _entry_to_paste(code: str, sites: tuple[str, ...]) -> str:
    return (
        f"  {code}: {{\n"
        f"    id: 'documents.processingIssue.code.{_camel_case(code)}',\n"
        f"    defaultMessage: '{code}',\n"
        f"    description: '<where it shows, and what the message raised at {', '.join(sites)} "
        "reports; ask for a short readable label>',\n"
        "  },"
    )


@pytest.mark.unit
def test_every_section_issue_code_has_a_ui_label():
    section_codes = _section_codes()
    missing = sorted(set(section_codes) - set(_ui_entries()))
    assert missing == [], (
        "the backend raises these processing issue codes on a section, but the UI's "
        "issueCodeMessages map has no label for them, so the Document Sections table "
        f"shows them as raw codes in every language: {json.dumps(missing)}\nAdd an "
        f"entry for each to {UI_MODULE.relative_to(REPO_ROOT)}, in alphabetical order. "
        "Its English is the code itself; write the description from the message the "
        "backend raises with it, as the other entries do:\n\n"
        + "\n".join(_entry_to_paste(code, section_codes[code]) for code in missing)
    )


@pytest.mark.unit
def test_the_ui_has_no_label_for_a_code_no_section_issue_carries():
    """A dead entry is translated and never shown."""
    unknown = sorted(set(_ui_entries()) - set(_section_codes()))
    assert unknown == [], (
        "the UI's issueCodeMessages map has labels for codes no backend "
        f"ProcessingIssue(...) raises on a section: {json.dumps(unknown)}\nIf the "
        "backend renamed a code, rename its entry (the key, its id and its "
        "defaultMessage). If the code is gone, or is raised only on the document "
        "(DOCUMENT_LEVEL_CODE_SITES), whose issues show by their message alone, "
        f"remove the entry from {UI_MODULE.relative_to(REPO_ROOT)}."
    )


@pytest.mark.unit
def test_each_label_is_the_code_itself_in_english():
    """English shows the stored code; a readable label is a translation's job."""
    wrong = {
        code: english for code, english in _ui_entries().items() if english != code
    }
    assert wrong == {}, (
        "these issueCodeMessages entries do not have their code as their English "
        f"(code -> defaultMessage, None when it is not one single-quoted line): {wrong}\n"
        f"Set each defaultMessage in {UI_MODULE.relative_to(REPO_ROOT)} to its code."
    )


@pytest.mark.unit
def test_both_sides_are_derived_at_full_strength():
    """A floor, not a truthiness check.

    Each direction above catches a code that one side alone loses: a code the walk
    misses fails the check for labels no section issue carries, and an entry the map
    read misses fails the check for codes without a label. This floor catches a code
    that drops out of both sides at once, when both readers weaken together or a code
    and its entry are deleted together. Raise these numbers when the tree gains codes;
    do not lower one without naming the code that stopped being raised.
    """
    section_codes = _section_codes()
    assert len(section_codes) >= 36, (
        f"derived only {len(section_codes)} section issue codes "
        f"({sorted(section_codes)}); the tree raises at least 36 on sections, so the "
        "walk is missing some"
    )
    assert len(_ui_entries()) >= 36, (
        f"read only {len(_ui_entries())} issueCodeMessages entries from "
        f"{UI_MODULE.relative_to(REPO_ROOT)}; the map has at least 36"
    )


@pytest.mark.unit
def test_no_new_unresolvable_code_site_appears():
    """A call site whose ``code`` no rule resolves joins neither side of the checks above.

    Fixing a failure here means one of two things: teach the walk the new expression
    shape, or add the site to ``UNRESOLVED_CODE_SITES`` with the reason it raises no
    code of its own.
    """
    found = _derive_backend()[1]
    expected = {site: count for site, (count, _why) in UNRESOLVED_CODE_SITES.items()}
    assert found == expected, (
        "the ProcessingIssue(...) call sites whose code this test cannot resolve "
        f"have changed.\n  found:    {dict(sorted(found.items()))}\n  expected: "
        f"{dict(sorted(expected.items()))}\nA site missing from the expected list "
        "raises codes this test never checks against the UI."
    )


@pytest.mark.unit
def test_no_new_document_level_site_appears():
    """A call without ``section_id=`` records a document's issue; its code joins neither side.

    Fixing a failure here means one of two things: add the site to
    ``DOCUMENT_LEVEL_CODE_SITES`` with what it records, or, if its issue belongs to a
    section, pass the call that section's ``section_id=``, so that its code is checked
    against the UI like the others.
    """
    found = _derive_backend()[2]
    expected = {
        site: count for site, (count, _why) in DOCUMENT_LEVEL_CODE_SITES.items()
    }
    assert found == expected, (
        "the ProcessingIssue(...) call sites without a section_id= have changed.\n"
        f"  found:    {dict(sorted(found.items()))}\n  expected: "
        f"{dict(sorted(expected.items()))}\nSuch an issue is recorded on the document, "
        "and the Document Details panel shows it by its message alone, so this test "
        "checks no label for its code. Pin a document-level site in "
        "DOCUMENT_LEVEL_CODE_SITES with what it records; give a section's issue its "
        "section_id=, so its code is checked against the UI."
    )
