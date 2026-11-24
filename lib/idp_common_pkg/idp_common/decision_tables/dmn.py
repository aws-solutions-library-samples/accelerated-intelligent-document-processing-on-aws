# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read and write a decision table as DMN XML.

The reader accepts the DMN 1.3, 1.4 and 1.5 model namespaces and maps one decision
that holds one decision table onto ``DecisionTable``. It refuses what this package
cannot represent (several decisions, chained decisions, business knowledge models,
decision logic other than a decision table) and names the construct. It does not
judge the table's content: the hit policy, the cells and the outputs are returned
as written, for the caller to accept or refuse.

The writer produces DMN 1.5 or DMN 1.3. The two differ in the namespaces and in
one rewrite: DMN 1.3 unary tests have no ``=`` or ``!=`` operator, so ``= x`` is
written ``x`` and ``!= x`` is written ``not(x)``.

Input is untrusted. It is size-checked before parsing and parsed with
``defusedxml`` with DTDs forbidden, so entity expansion and external entities are
refused. ``defusedxml`` is imported inside the functions that parse, so importing
this module costs nothing and the rest of the package never needs it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # nosec B405 - used to BUILD a tree, never to parse
from dataclasses import dataclass, field
from typing import Optional, Union
from xml.sax.saxutils import escape, quoteattr  # nosec B406 - escaping only

from .models import (
    DecisionTable,
    DecisionTableError,
    TableInput,
    TableOutput,
    TableRule,
)
from .sfeel import to_dmn13

#: DMN version to its model namespace. The reader accepts every one of them.
DMN_NAMESPACES = {
    "1.3": "https://www.omg.org/spec/DMN/20191111/MODEL/",
    "1.4": "https://www.omg.org/spec/DMN/20211108/MODEL/",
    "1.5": "https://www.omg.org/spec/DMN/20230324/MODEL/",
}
#: The diagram-interchange namespace each version's files use.
_DMNDI_NAMESPACES = {
    "1.3": "https://www.omg.org/spec/DMN/20191111/DMNDI/",
    "1.5": "https://www.omg.org/spec/DMN/20230324/DMNDI/",
}
_DC_NAMESPACE = "http://www.omg.org/spec/DMN/20180521/DC/"

#: Versions the writer produces.
WRITABLE_VERSIONS = ("1.5", "1.3")

MAX_DMN_BYTES = 512 * 1024

#: The ``namespace`` a written file's ``definitions`` carries unless told otherwise.
DEFAULT_TARGET_NAMESPACE = "urn:aws:idp:decision-tables:1"

#: Decision logic a decision can hold instead of a decision table.
_OTHER_DECISION_LOGIC = (
    "literalExpression",
    "context",
    "invocation",
    "relation",
    "list",
    "functionDefinition",
    "conditional",
    "for",
    "every",
    "some",
    "filter",
)

#: Characters XML 1.0 cannot carry, even escaped.
_NOT_XML_TEXT = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


class DmnImportError(DecisionTableError):
    """DMN XML that is refused. ``construct`` names what caused the refusal."""

    def __init__(self, message: str, *, construct: str) -> None:
        super().__init__(message)
        self.construct = construct


@dataclass(frozen=True)
class DmnDocument:
    """A decision table read from DMN XML, with what the model does not hold."""

    table: DecisionTable
    #: The DMN version of the file's namespace: ``"1.3"``, ``"1.4"`` or ``"1.5"``.
    version: str
    #: The decision table's ``aggregation`` attribute, empty when absent.
    aggregation: str = ""
    #: Attributes on ``decisionTable`` from other namespaces, by ``{uri}name``.
    table_attributes: dict[str, str] = field(default_factory=dict)
    #: The same for each ``rule``, by rule id.
    rule_attributes: dict[str, dict[str, str]] = field(default_factory=dict)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _foreign(attributes: dict[str, str]) -> dict[str, str]:
    return {name: value for name, value in attributes.items() if name[:1] == "{"}


def _parse(xml: Union[str, bytes]) -> ET.Element:
    size = len(xml) if isinstance(xml, bytes) else len(xml.encode("utf-8"))
    if size > MAX_DMN_BYTES:
        raise DmnImportError(
            f"The DMN file is {size} bytes; the limit is {MAX_DMN_BYTES} bytes",
            construct="size",
        )
    import defusedxml.ElementTree as SafeET
    from defusedxml import DefusedXmlException

    try:
        return SafeET.fromstring(xml, forbid_dtd=True)
    except DefusedXmlException as error:
        raise DmnImportError(
            "The DMN file declares a DTD or an entity, which is not accepted "
            f"({type(error).__name__})",
            construct="DTD",
        ) from None
    except ET.ParseError as error:
        raise DmnImportError(
            f"The DMN file is not well-formed XML: {error}", construct="XML"
        ) from None


def _text(element: Optional[ET.Element], namespace: str) -> str:
    """The content of ``element``'s ``text`` child, stripped; empty when absent."""
    if element is None:
        return ""
    child = element.find(f"{{{namespace}}}text")
    return (child.text or "").strip() if child is not None else ""


def read_dmn(xml: Union[str, bytes]) -> DmnDocument:
    """Read one decision table, with its version and its foreign attributes.

    Raises:
        DmnImportError: naming the construct, when the input is too large, is not
            well-formed, declares a DTD or an entity, is in an unknown namespace,
            or holds something other than one decision with one decision table.
    """
    root = _parse(xml)
    namespace = root.tag[1:].split("}", 1)[0] if root.tag[:1] == "{" else ""
    versions = {uri: version for version, uri in DMN_NAMESPACES.items()}
    if namespace not in versions:
        raise DmnImportError(
            f"The DMN file is in the namespace {namespace!r}, which is not a DMN "
            f"1.3, 1.4 or 1.5 model namespace",
            construct="namespace",
        )
    if _local(root.tag) != "definitions":
        raise DmnImportError(
            f"The root element is {_local(root.tag)!r}; a DMN file starts with "
            f"definitions",
            construct=_local(root.tag),
        )

    def q(name: str) -> str:
        return f"{{{namespace}}}{name}"

    if root.find(q("businessKnowledgeModel")) is not None:
        raise DmnImportError(
            "The DMN file has a businessKnowledgeModel, which is not supported",
            construct="businessKnowledgeModel",
        )
    decisions = root.findall(q("decision"))
    if len(decisions) != 1:
        raise DmnImportError(
            f"The DMN file has {len(decisions)} decision elements; exactly one "
            f"decision is supported",
            construct="decision",
        )
    decision = decisions[0]
    if decision.find(q("knowledgeRequirement")) is not None:
        raise DmnImportError(
            "The decision has a knowledgeRequirement, which is not supported",
            construct="knowledgeRequirement",
        )
    for requirement in decision.findall(q("informationRequirement")):
        if requirement.find(q("requiredDecision")) is not None:
            raise DmnImportError(
                "The decision requires another decision (informationRequirement "
                "with requiredDecision); chained decisions are not supported",
                construct="requiredDecision",
            )
    table_element = decision.find(q("decisionTable"))
    if table_element is None:
        found = next(
            (
                _local(child.tag)
                for child in decision
                if _local(child.tag) in _OTHER_DECISION_LOGIC
            ),
            "",
        )
        raise DmnImportError(
            (
                f"The decision's logic is a {found}; only a decisionTable is supported"
                if found
                else "The decision has no decisionTable"
            ),
            construct=found or "decisionTable",
        )

    decision_id = decision.get("id") or table_element.get("id") or "decision"
    inputs = []
    for position, element in enumerate(table_element.findall(q("input")), start=1):
        expression = element.find(q("inputExpression"))
        inputs.append(
            TableInput(
                id=element.get("id") or f"input_{position}",
                label=element.get("label") or "",
                expression=_text(expression, namespace),
                type_ref=(expression.get("typeRef") if expression is not None else "")
                or "",
                allowed_values=_text(element.find(q("inputValues")), namespace),
            )
        )
    outputs = [
        TableOutput(
            id=element.get("id") or f"output_{position}",
            name=element.get("name") or "",
            type_ref=element.get("typeRef") or "",
            default=_text(element.find(q("defaultOutputEntry")), namespace),
        )
        for position, element in enumerate(table_element.findall(q("output")), start=1)
    ]
    # A table with one output column need not name it: the value is the
    # decision's, so it takes the decision's name.
    if len(outputs) == 1 and not outputs[0].name:
        outputs[0].name = (
            table_element.get("outputLabel") or decision.get("name") or decision_id
        )
    rules = []
    rule_attributes: dict[str, dict[str, str]] = {}
    for position, element in enumerate(table_element.findall(q("rule")), start=1):
        rule_id = element.get("id") or f"rule_{position}"
        description = element.find(q("description"))
        rules.append(
            TableRule(
                id=rule_id,
                description=(description.text or "").strip()
                if description is not None
                else "",
                # Modelers write an empty cell for "any value".
                input_entries=[
                    _text(entry, namespace) or "-"
                    for entry in element.findall(q("inputEntry"))
                ],
                output_entries=[
                    _text(entry, namespace)
                    for entry in element.findall(q("outputEntry"))
                ],
            )
        )
        foreign = _foreign(element.attrib)
        if foreign:
            rule_attributes[rule_id] = foreign

    return DmnDocument(
        table=DecisionTable(
            id=decision_id,
            name=decision.get("name") or "",
            # DMN's default hit policy.
            hit_policy=table_element.get("hitPolicy") or "UNIQUE",
            inputs=inputs,
            outputs=outputs,
            rules=rules,
        ),
        version=versions[namespace],
        aggregation=table_element.get("aggregation") or "",
        table_attributes=_foreign(table_element.attrib),
        rule_attributes=rule_attributes,
    )


def read_decision_table(xml: Union[str, bytes]) -> DecisionTable:
    """The decision table in DMN XML. See :func:`read_dmn` for what is refused."""
    return read_dmn(xml).table


def _xml_text(value: str, where: str) -> str:
    if _NOT_XML_TEXT.search(value):
        raise DecisionTableError(
            f"{where} holds a control character that XML cannot carry"
        )
    return value


def _serialise(root: ET.Element, prefixes: dict[str, str]) -> str:
    """``root`` as indented XML, with the given namespace prefixes.

    ``prefixes`` maps a namespace to its prefix; the empty prefix is the default
    namespace. ElementTree's own writer takes prefixes from a process-wide
    registry, which a library must not rewrite, so the few lines it takes to
    choose them here are written out.
    """

    def name(qualified: str, *, attribute: bool) -> str:
        if qualified[:1] != "{":
            return qualified
        uri, local = qualified[1:].split("}", 1)
        prefix = prefixes.get(uri)
        if prefix is None or (attribute and not prefix):
            raise DecisionTableError(
                f"No namespace prefix is declared for {uri!r} ({local})"
            )
        return f"{prefix}:{local}" if prefix else local

    quoted = {"\n": "&#10;", "\r": "&#13;", "\t": "&#9;"}
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']

    def emit(element: ET.Element, depth: int) -> None:
        tag = name(element.tag, attribute=False)
        attributes = []
        if depth == 0:
            attributes = [
                (f"xmlns:{prefix}" if prefix else "xmlns", uri)
                for uri, prefix in prefixes.items()
            ]
        attributes += [
            (name(key, attribute=True), _xml_text(value, f"The attribute {key}"))
            for key, value in element.attrib.items()
        ]
        opening = "  " * depth + "<" + tag
        opening += "".join(
            f" {key}={quoteattr(value, quoted)}" for key, value in attributes
        )
        children = list(element)
        text = _xml_text(element.text or "", f"The element {tag}")
        if children:
            lines.append(opening + ">")
            for child in children:
                emit(child, depth + 1)
            lines.append("  " * depth + f"</{tag}>")
        elif text:
            lines.append(f"{opening}>{escape(text)}</{tag}>")
        else:
            lines.append(opening + "/>")

    emit(root, 0)
    return "\n".join(lines) + "\n"


def write_decision_table(
    table: DecisionTable,
    *,
    dmn_version: str = "1.5",
    extra_table_attributes: Optional[dict[str, str]] = None,
    rule_attributes: Optional[dict[str, dict[str, str]]] = None,
    namespace_prefixes: Optional[dict[str, str]] = None,
    target_namespace: str = DEFAULT_TARGET_NAMESPACE,
) -> str:
    """``table`` as a DMN file: one decision holding one decision table.

    Args:
        table: The table. Its ``id`` and ``name`` become the decision's.
        dmn_version: ``"1.5"`` or ``"1.3"``. The 1.3 form is what Camunda Modeler
            and dmn-js open; it writes ``= x`` as ``x`` and ``!= x`` as ``not(x)``.
        extra_table_attributes: Attributes for ``decisionTable`` from another
            namespace, by ``{uri}name``.
        rule_attributes: The same for a ``rule``, by rule id.
        namespace_prefixes: Prefix to namespace, for every namespace those
            attributes use.
        target_namespace: The ``namespace`` attribute of ``definitions``, which
            identifies the model to other DMN tools.

    Raises:
        DecisionTableError: an unknown version, a test DMN 1.3 cannot express, an
            attribute namespace with no prefix, or text XML cannot carry.
    """
    if dmn_version not in WRITABLE_VERSIONS:
        raise DecisionTableError(
            f"DMN version {dmn_version!r} cannot be written; choose one of "
            f"{', '.join(WRITABLE_VERSIONS)}"
        )
    namespace = DMN_NAMESPACES[dmn_version]
    dmndi = _DMNDI_NAMESPACES[dmn_version]
    legacy = dmn_version == "1.3"

    def q(name: str) -> str:
        return f"{{{namespace}}}{name}"

    def entry_text(entry: str) -> str:
        return to_dmn13(entry) if legacy else entry

    name = table.name or table.id
    root = ET.Element(
        q("definitions"),
        {
            "id": f"definitions_{table.id}",
            "name": name,
            "namespace": target_namespace,
        },
    )
    decision = ET.SubElement(root, q("decision"), {"id": table.id, "name": name})
    table_element = ET.SubElement(
        decision,
        q("decisionTable"),
        {"id": f"{table.id}_table", "hitPolicy": table.hit_policy},
    )
    for key, value in (extra_table_attributes or {}).items():
        table_element.set(key, value)

    for column in table.inputs:
        attributes = {"id": column.id}
        if column.label:
            attributes["label"] = column.label
        element = ET.SubElement(table_element, q("input"), attributes)
        expression_attributes = {"id": f"{column.id}_expression"}
        if column.type_ref:
            expression_attributes["typeRef"] = column.type_ref
        expression = ET.SubElement(element, q("inputExpression"), expression_attributes)
        ET.SubElement(expression, q("text")).text = column.expression
        if column.allowed_values:
            values = ET.SubElement(element, q("inputValues"))
            ET.SubElement(values, q("text")).text = entry_text(column.allowed_values)

    for column in table.outputs:
        attributes = {"id": column.id}
        if column.name:
            attributes["name"] = column.name
        if column.type_ref:
            attributes["typeRef"] = column.type_ref
        element = ET.SubElement(table_element, q("output"), attributes)
        if column.default:
            default = ET.SubElement(element, q("defaultOutputEntry"))
            ET.SubElement(default, q("text")).text = column.default

    for rule in table.rules:
        element = ET.SubElement(table_element, q("rule"), {"id": rule.id})
        for key, value in (rule_attributes or {}).get(rule.id, {}).items():
            element.set(key, value)
        if rule.description:
            ET.SubElement(element, q("description")).text = rule.description
        for position, entry in enumerate(rule.input_entries, start=1):
            cell = ET.SubElement(
                element, q("inputEntry"), {"id": f"{rule.id}_in_{position}"}
            )
            ET.SubElement(cell, q("text")).text = entry_text(entry)
        for position, entry in enumerate(rule.output_entries, start=1):
            cell = ET.SubElement(
                element, q("outputEntry"), {"id": f"{rule.id}_out_{position}"}
            )
            ET.SubElement(cell, q("text")).text = entry

    # The smallest diagram a graphical modeler needs to show the decision.
    diagram = ET.SubElement(
        ET.SubElement(root, f"{{{dmndi}}}DMNDI"),
        f"{{{dmndi}}}DMNDiagram",
        {"id": f"diagram_{table.id}"},
    )
    shape = ET.SubElement(
        diagram,
        f"{{{dmndi}}}DMNShape",
        {"id": f"shape_{table.id}", "dmnElementRef": table.id},
    )
    ET.SubElement(
        shape,
        f"{{{_DC_NAMESPACE}}}Bounds",
        {"height": "80", "width": "180", "x": "160", "y": "100"},
    )

    prefixes = {namespace: "", dmndi: "dmndi", _DC_NAMESPACE: "dc"}
    for prefix, uri in (namespace_prefixes or {}).items():
        prefixes[uri] = prefix
    return _serialise(root, prefixes)
