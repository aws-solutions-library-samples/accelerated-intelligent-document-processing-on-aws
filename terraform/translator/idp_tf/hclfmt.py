"""Canonical layout for the HCL the translator writes, as ``terraform fmt`` lays it out.

The emitters build HCL as text with a fixed two-space indent and a single space
around every ``=``. ``terraform fmt`` differs from that in three ways, and this
module applies exactly those three, so a module written through
:func:`format_hcl` is a fixed point of ``terraform fmt``:

1. **Alignment.** The ``=`` of consecutive single-line attributes (and object
   items) is padded to one column, as are trailing comments on consecutive lines.
2. **Indentation.** Each line is indented by the bracket nesting open before it,
   counted the way ``hclwrite`` counts it, including its treatment of a line that
   both closes and opens brackets (``] : []), (cond ? [``).
3. **Interpolation-only values.** An attribute whose whole value is one
   ``"${ ... }"`` sequence is unwrapped to the bare expression, which is the one
   token-level rewrite ``terraform fmt`` makes beyond ``hclwrite.Format``.

The rules are ports of ``hclwrite``'s ``formatIndent`` and ``formatCells`` and of
``terraform fmt``'s ``formatValueExpr``; see those for the authority. What is
**not** ported is ``hclwrite``'s token spacing (``formatSpaces``): the spacing
between tokens on a line is kept as written, because the emitters already write
it canonically. Nor are ``variable`` type expressions rewritten (``"string"`` to
``string``): the emitters write the modern form. Content inside quoted strings
and heredocs is never changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Token kinds. The bracket-like ones mirror hclsyntax's token types, because the
# indentation and alignment rules are defined over them.
NEWLINE = "newline"
COMMENT = "comment"
OQUOTE = "oquote"
CQUOTE = "cquote"
QUOTED_LIT = "quoted_lit"
INTERP = "interp"  # ${
CONTROL = "control"  # %{
SEQ_END = "seq_end"  # the } (or ~}) closing an interp or control sequence
HEREDOC = "heredoc"  # the whole <<EOT ... EOT, opaque
OBRACE, CBRACE = "obrace", "cbrace"
OBRACK, CBRACK = "obrack", "cbrack"
OPAREN, CPAREN = "oparen", "cparen"
EQUAL = "equal"  # a lone `=`, not `==`, `<=`, `>=`, `!=` or `=>`
IDENT = "ident"
OTHER = "other"

_OPENERS = {OBRACE, OBRACK, OPAREN, INTERP, CONTROL}
_CLOSERS = {CBRACE, CBRACK, CPAREN, SEQ_END}
_SINGLE = {"{": OBRACE, "}": CBRACE, "[": OBRACK, "]": CBRACK, "(": OPAREN, ")": CPAREN}

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_SPACE = re.compile(r"[ \t]+")
_OPERATOR = re.compile(r"==|!=|<=|>=|=>|&&|\|\||\.\.\.|[-+*/%<>!?:.,]")
_HEREDOC_OPEN = re.compile(r"<<(-?)([A-Za-z_][A-Za-z0-9_-]*)\r?\n")


class HclFormatError(ValueError):
    """The text is not HCL this formatter can read."""


@dataclass
class Token:
    kind: str
    start: int
    end: int


def _bracket_change(tok: Token) -> int:
    if tok.kind in _OPENERS:
        return 1
    if tok.kind in _CLOSERS:
        return -1
    return 0


class _Lexer:
    """Enough of the HCL native-syntax lexer to find brackets, strings and ``=``."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.tokens: list[Token] = []

    def emit(self, kind: str, end: int) -> None:
        self.tokens.append(Token(kind, self.pos, end))
        self.pos = end

    def run(self) -> list[Token]:
        self.expression(stop_at_brace=False)
        if self.pos < len(self.text):
            raise HclFormatError(f"unbalanced '}}' at offset {self.pos}")
        return self.tokens

    def expression(self, stop_at_brace: bool) -> None:
        """Lex until EOF, or (inside a template sequence) its closing brace."""
        text = self.text
        depth = 0
        while self.pos < len(text):
            c = text[self.pos]
            if c in " \t":
                m = _SPACE.match(text, self.pos)
                assert m is not None
                self.pos = m.end()
            elif c == "\n":
                self.emit(NEWLINE, self.pos + 1)
            elif c == "\r" and text.startswith("\r\n", self.pos):
                self.emit(NEWLINE, self.pos + 2)
            elif c == "#" or text.startswith("//", self.pos):
                nl = text.find("\n", self.pos)
                self.emit(COMMENT, len(text) if nl < 0 else nl + 1)
            elif text.startswith("/*", self.pos):
                close = text.find("*/", self.pos + 2)
                if close < 0:
                    raise HclFormatError(f"unterminated comment at offset {self.pos}")
                self.emit(COMMENT, close + 2)
            elif c == '"':
                self.quoted()
            elif text.startswith("<<", self.pos) and _HEREDOC_OPEN.match(
                text, self.pos
            ):
                self.heredoc()
            elif c == "{":
                depth += 1
                self.emit(OBRACE, self.pos + 1)
            elif c == "}" or (c == "~" and text.startswith("~}", self.pos)):
                if depth == 0:
                    if stop_at_brace:
                        return
                    raise HclFormatError(f"unbalanced '}}' at offset {self.pos}")
                depth -= 1
                self.emit(CBRACE, self.pos + 1)
            elif c in _SINGLE:
                self.emit(_SINGLE[c], self.pos + 1)
            elif c == "=" and not _OPERATOR.match(text, self.pos):
                self.emit(EQUAL, self.pos + 1)
            elif m := _IDENT.match(text, self.pos):
                self.emit(IDENT, m.end())
            elif m := _NUMBER.match(text, self.pos):
                self.emit(OTHER, m.end())
            elif m := _OPERATOR.match(text, self.pos):
                self.emit(OTHER, m.end())
            else:
                raise HclFormatError(f"unexpected {c!r} at offset {self.pos}")
        if stop_at_brace:
            raise HclFormatError("unterminated template sequence")

    def sequence(self, kind: str) -> None:
        """``${`` / ``%{`` (optionally ``~``), the expression, then its ``}``."""
        width = 3 if self.text.startswith("~", self.pos + 2) else 2
        self.emit(kind, self.pos + width)
        self.expression(stop_at_brace=True)
        width = 2 if self.text.startswith("~}", self.pos) else 1
        self.emit(SEQ_END, self.pos + width)

    def quoted(self) -> None:
        text = self.text
        self.emit(OQUOTE, self.pos + 1)
        lit_start = self.pos
        while True:
            if self.pos >= len(text) or text[self.pos] == "\n":
                raise HclFormatError("unterminated quoted string")
            c = text[self.pos]
            if c == "\\":
                self.pos += 2
            elif text.startswith(("$${", "%%{"), self.pos):
                self.pos += 3
            elif text.startswith(("${", "%{"), self.pos):
                if self.pos > lit_start:
                    self.tokens.append(Token(QUOTED_LIT, lit_start, self.pos))
                self.sequence(INTERP if c == "$" else CONTROL)
                lit_start = self.pos
            elif c == '"':
                if self.pos > lit_start:
                    self.tokens.append(Token(QUOTED_LIT, lit_start, self.pos))
                self.emit(CQUOTE, self.pos + 1)
                return
            else:
                self.pos += 1

    def heredoc(self) -> None:
        m = _HEREDOC_OPEN.match(self.text, self.pos)
        assert m is not None
        marker = m.group(2)
        line_start = m.end()
        while True:
            nl = self.text.find("\n", line_start)
            line_end = len(self.text) if nl < 0 else nl
            if self.text[line_start:line_end].strip() == marker:
                self.emit(HEREDOC, line_end)
                return
            if nl < 0:
                raise HclFormatError(f"unterminated heredoc {marker}")
            line_start = nl + 1


def _is_line_end(tok: Token, text: str) -> bool:
    return tok.kind == NEWLINE or (
        tok.kind == COMMENT and text[tok.start : tok.end].endswith("\n")
    )


# --- terraform fmt's formatValueExpr --------------------------------------


def _unwrap_spans(text: str, tokens: list[Token]) -> list[tuple[int, int, str]]:
    """(start, end, replacement) for every attribute whose value is ``"${x}"``.

    Only attributes of a body (the file, or a block's body) qualify: an item of
    an object constructor is an expression, and ``terraform fmt`` leaves those.
    """
    spans: list[tuple[int, int, str]] = []
    # Stack of contexts, one per open bracket: True for a block body.
    stack: list[bool] = [True]
    statement_start = True  # at the first token of a body statement
    header = False  # inside a block header (labels before `{`)
    value_from: int | None = None  # token index where an attribute value starts
    value_depth = 0
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if value_from is not None and len(stack) == value_depth:
            if _is_line_end(tok, text) or tok.kind == CBRACE:
                spans.extend(_unwrap(text, tokens[value_from:i]))
                value_from = None
        if tok.kind in (NEWLINE, COMMENT) and stack[-1]:
            if _is_line_end(tok, text):
                statement_start, header = True, False
            i += 1
            continue
        if statement_start and stack[-1]:
            statement_start = False
            if (
                tok.kind == IDENT
                and i + 1 < n
                and tokens[i + 1].kind == EQUAL
                and value_from is None
            ):
                value_from, value_depth = i + 2, len(stack)
                i += 2
                continue
            header = tok.kind == IDENT
        if tok.kind in _OPENERS:
            stack.append(tok.kind == OBRACE and header and stack[-1])
            if stack[-1]:
                statement_start, header = True, False
        elif tok.kind in _CLOSERS:
            if len(stack) == 1:
                raise HclFormatError(f"unbalanced closer at offset {tok.start}")
            closed_body = stack.pop()
            if closed_body:
                statement_start = False
        i += 1
    if value_from is not None:
        spans.extend(_unwrap(text, tokens[value_from:]))
    return spans


def _unwrap(text: str, expr: list[Token]) -> list[tuple[int, int, str]]:
    if len(expr) < 5:
        return []
    if (
        expr[0].kind != OQUOTE
        or expr[1].kind != INTERP
        or expr[-2].kind != SEQ_END
        or expr[-1].kind != CQUOTE
    ):
        return []
    inside = expr[2:-2]
    quotes = 0
    for tok in inside:
        if tok.kind == OQUOTE:
            quotes += 1
        elif tok.kind == CQUOTE:
            quotes -= 1
        elif quotes > 0:
            continue
        elif tok.kind in (INTERP, CONTROL, SEQ_END, QUOTED_LIT):
            return []
    while inside and inside[0].kind == NEWLINE:
        inside = inside[1:]
    while inside and inside[-1].kind == NEWLINE:
        inside = inside[:-1]
    if not inside:
        return []
    body = text[inside[0].start : inside[-1].end]
    multiline = any(t.kind == NEWLINE for t in inside)
    if multiline and not (inside[0].kind == OPAREN and inside[-1].kind == CPAREN):
        body = f"({body})"
    return [(expr[0].start, expr[-1].end, body)]


def _apply(text: str, spans: list[tuple[int, int, str]]) -> str:
    out, last = [], 0
    for start, end, repl in sorted(spans):
        out += [text[last:start], repl]
        last = end
    out.append(text[last:])
    return "".join(out)


# --- hclwrite's formatIndent and formatCells --------------------------------


@dataclass
class _Line:
    lead: list[Token]
    assign: list[Token]
    comment: list[Token]


def _lines(text: str, tokens: list[Token]) -> list[_Line]:
    lines: list[_Line] = []
    current: list[Token] = []
    for tok in tokens:
        current.append(tok)
        if _is_line_end(tok, text):
            lines.append(_Line(current, [], []))
            current = []
    lines.append(_Line(current, [], []))
    for line in lines:
        lead = line.lead
        if len(lead) > 1 and lead[-1].kind == COMMENT:
            line.comment, lead = lead[-1:], lead[:-1]
        for idx, tok in enumerate(lead):
            if idx > 0 and tok.kind == EQUAL:
                net = 0
                for t in lead[idx:]:
                    net += _bracket_change(t)
                    if t.kind == HEREDOC:
                        break
                if net == 0:
                    line.assign, lead = lead[idx:], lead[:idx]
                break
        line.lead = lead
    return lines


def _indents(lines: list[_Line]) -> list[int]:
    out: list[int] = []
    stack: list[int] = []
    for line in lines:
        if not line.lead or line.lead[0].kind == NEWLINE:
            out.append(0)
            continue
        net = 0
        for cell in (line.lead, line.assign):
            for t in cell:
                net += _bracket_change(t)
                if t.kind == HEREDOC:
                    break
        if net > 0:
            out.append(2 * len(stack))
            stack.append(net)
            continue
        closed = -net
        while closed > 0 and stack:
            if closed > stack[-1]:
                closed -= stack.pop()
            elif closed < stack[-1]:
                stack[-1] -= closed
                closed = 0
            else:
                stack.pop()
                closed = 0
        out.append(2 * len(stack))
    return out


def _join(text: str, toks: list[Token]) -> str:
    """The tokens' text with the spacing between them kept and none around them."""
    if not toks:
        return ""
    body = text[toks[0].start : toks[-1].end]
    return body[:-1] if body.endswith("\n") else body


def _layout(text: str) -> str:
    tokens = _Lexer(text).run()
    lines = _lines(text, tokens)
    indents = _indents(lines)

    def lead_text(k: int) -> str:
        line = lines[k]
        return " " * indents[k] + _join(text, line.lead).rstrip(" \t")

    def assign_text(k: int) -> str:
        return _join(text, lines[k].assign).rstrip(" \t")

    assign_pad = [1] * len(lines)
    for chain in _chains([bool(line.assign) for line in lines]):
        width = max(len(lead_text(k)) for k in chain)
        for k in chain:
            assign_pad[k] = width - len(lead_text(k)) + 1

    def before_comment(k: int) -> str:
        if not lines[k].assign:
            return lead_text(k)
        return lead_text(k) + " " * assign_pad[k] + assign_text(k)

    comment_pad = [1] * len(lines)
    for chain in _chains([bool(line.comment) for line in lines]):
        width = max(len(before_comment(k)) for k in chain)
        for k in chain:
            comment_pad[k] = width - len(before_comment(k)) + 1

    out = []
    for k, line in enumerate(lines):
        if not line.lead and not line.comment:
            ending = "" if k == len(lines) - 1 else "\n"
            out.append(ending)
            continue
        if line.lead and line.lead[0].kind == NEWLINE:
            out.append("\n")
            continue
        row = before_comment(k)
        if line.comment:
            row += " " * comment_pad[k] + _join(text, line.comment).rstrip(" \t")
        ends = (line.comment or line.assign or line.lead)[-1]
        out.append(row + ("\n" if _is_line_end(ends, text) else ""))
    return "".join(out)


def _chains(flags: list[bool]) -> list[list[int]]:
    chains: list[list[int]] = []
    current: list[int] = []
    for k, flag in enumerate(flags):
        if flag:
            current.append(k)
        elif current:
            chains.append(current)
            current = []
    if current:
        chains.append(current)
    return chains


def format_hcl(text: str) -> str:
    """``text`` laid out as ``terraform fmt`` lays it out (see the module doc)."""
    spans = _unwrap_spans(text, _Lexer(text).run())
    if spans:
        text = _apply(text, spans)
    return _layout(text)
