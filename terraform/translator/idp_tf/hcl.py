"""Small helpers for writing HCL text. Expressions are plain strings."""

from __future__ import annotations

import re

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def snake(name: str) -> str:
    """CamelCase logical id to a Terraform-style identifier."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    return s.lower()


def norm(name: str) -> str:
    """Comparison key shared by CloudFormation and awscc attribute names."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def escape_template(text: str) -> str:
    """Escape literal text for an HCL quoted template."""
    out = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return out.replace("${", "$${").replace("%{", "%%{")


def string(text: str) -> str:
    return '"' + escape_template(text) + '"'


def key(name: str) -> str:
    return name if IDENT.match(name) else string(name)


def indent(text: str, by: int = 2) -> str:
    pad = " " * by
    return "\n".join(pad + line if line else line for line in text.split("\n"))


def obj(items: list[tuple[str, str]], quote_keys: bool = False) -> str:
    if not items:
        return "{}"
    lines = []
    for k, v in items:
        lines.append(f"{string(k) if quote_keys else key(k)} = {v}")
    return "{\n" + indent("\n".join(lines)) + "\n}"


def lst(items: list[str]) -> str:
    if not items:
        return "[]"
    if all("\n" not in i for i in items) and sum(len(i) for i in items) < 80:
        return "[" + ", ".join(items) + "]"
    return "[\n" + indent(",\n".join(items)) + ",\n]"


def block(header: str, body_lines: list[str]) -> str:
    body = "\n".join(body_lines)
    return header + " {\n" + indent(body) + "\n}\n"
