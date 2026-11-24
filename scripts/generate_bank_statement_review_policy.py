# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generate the synthetic policy document of the rule validation sample.

Policy rule validation checks a processed document against policy rules, and
Policy Discovery extracts such rules from a policy document. The
``rule-validation`` preset shows both with one domain: reviewing a bank
statement. This script renders the policy document of that sample,

    samples/rule-validation/bank-statement-review-policy.pdf

a short policy of a fictitious organisation that says what a bank statement
must show before it is accepted. The document to validate against it is
``samples/bank-statement-multipage.pdf``.

``SECTIONS`` below is the policy: one section per policy type, and under it one
policy rule per identifier. The ``rule-validation`` preset's ``policy_classes``
hold the same policy types, policy rule identifiers and texts, and
``scripts/tests/test_generate_bank_statement_review_policy.py`` fails when the
two differ, or when the shipped PDF does not print them.

Deterministic: no random values, and the PDF is built in reportlab's invariant
mode (a fixed creation date and document id), so a second run with the same
reportlab version writes the same bytes.

Usage::

    python scripts/generate_bank_statement_review_policy.py \
        --out-dir samples/rule-validation

writes ``bank-statement-review-policy.pdf`` into that directory.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, List, NamedTuple, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

POLICY_NAME = "bank-statement-review-policy.pdf"

# Fictitious names only (AWS docs convention: AnyCompany / Example).
ORGANISATION = "AnyCompany Financial Services"
TITLE_TEXT = "Bank Statement Review Policy"
POLICY_ID = "Policy BSR-01, version 1.0"

INTRODUCTION = [
    "This policy says what a bank statement must show before AnyCompany "
    "Financial Services accepts it as evidence of an account and its activity. "
    "A reviewer, or an automated check, reads the statement against every "
    "policy rule below.",
    "Each policy rule has an identifier and one requirement. A statement passes "
    "a policy rule when it meets the requirement, and fails it in the case the "
    "policy rule names. When the statement does not hold the information the "
    "policy rule needs, the policy rule is recorded as not found.",
]


class Rule(NamedTuple):
    """One policy rule: its identifier, its heading and its requirement."""

    id: str
    heading: str
    text: str


class Section(NamedTuple):
    """One policy type: its identifier, its heading and its policy rules."""

    policy_type: str
    heading: str
    rules: Tuple[Rule, ...]


SECTIONS: Tuple[Section, ...] = (
    Section(
        "statement_identity",
        "Statement identity",
        (
            Rule(
                "institution_named",
                "Institution named",
                "The statement must name the financial institution that issued "
                "it. The rule fails when no institution name is printed on the "
                "statement.",
            ),
            Rule(
                "account_holder_named",
                "Account holder named",
                "The statement must show the full name and the mailing address "
                "of the account holder. The rule fails when the name or the "
                "address is not printed on the statement.",
            ),
            Rule(
                "account_number_shown",
                "Account number shown",
                "The statement must show an account number for every account it "
                "summarises. The rule fails when an account is listed without "
                "an account number.",
            ),
        ),
    ),
    Section(
        "statement_period",
        "Statement period",
        (
            Rule(
                "statement_date_shown",
                "Statement date shown",
                "The statement must show the date it was issued or the period "
                "it covers. The rule fails when the statement prints neither.",
            ),
            Rule(
                "balance_dates_in_order",
                "Balance dates in order",
                "The date printed for the new balance of an account must be the "
                "same as or later than the date printed for its previous "
                "balance. The rule fails when the new balance is dated before "
                "the previous balance.",
            ),
            Rule(
                "transactions_within_period",
                "Transactions within the period",
                "Every transaction listed on the statement must be dated within "
                "the period the statement covers and no later than the "
                "statement date. The rule fails when a transaction is dated "
                "outside that period.",
            ),
        ),
    ),
    Section(
        "balance_reconciliation",
        "Balance reconciliation",
        (
            Rule(
                "new_balance_reconciles",
                "New balance reconciles",
                "For each account, the new balance must equal the previous "
                "balance, minus checks and other withdrawals, plus deposits and "
                "credits, using the amounts printed in the account summary. The "
                "rule fails when the printed amounts do not add up to the "
                "printed new balance.",
            ),
            Rule(
                "summary_total_matches_accounts",
                "Summary total matches the accounts",
                "The total printed for a group of accounts in the summary of "
                "accounts must equal the sum of the balances of the accounts in "
                "that group. The rule fails when the total differs from that "
                "sum.",
            ),
        ),
    ),
    Section(
        "transaction_review",
        "Transaction review",
        (
            Rule(
                "transactions_in_date_order",
                "Transactions in date order",
                "Transactions must be listed in date order, oldest first, "
                "within each account. The rule fails when a transaction is "
                "listed after a transaction with a later date.",
            ),
            Rule(
                "no_duplicate_transactions",
                "No duplicate transactions",
                "A transaction must be listed once. The rule fails when two "
                "listed transactions have the same date, description, reference "
                "number and amount.",
            ),
            Rule(
                "large_withdrawals_identified",
                "Large withdrawals identified",
                "Every withdrawal of 10,000.00 or more must have a description "
                "that identifies the payee. The rule fails when such a "
                "withdrawal has no description, or a description that names no "
                "payee.",
            ),
        ),
    ),
)

CLOSING = (
    "This is a synthetic policy written for the samples of the GenAI Intelligent "
    "Document Processing accelerator. AnyCompany Financial Services is a "
    "fictitious organisation."
)

MARGIN = 0.9 * inch

_styles = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=_styles["Normal"], fontSize=10.5, leading=14.5)
TITLE = ParagraphStyle(
    "title", parent=BODY, fontSize=22, leading=26, fontName="Helvetica-Bold"
)
SUBTITLE = ParagraphStyle(
    "subtitle", parent=BODY, fontSize=11, textColor=colors.HexColor("#333333")
)
SECTION = ParagraphStyle(
    "section", parent=BODY, fontSize=14, leading=18, fontName="Helvetica-Bold"
)
RULE = ParagraphStyle("rule", parent=BODY, fontName="Helvetica-Bold")
SMALL = ParagraphStyle(
    "small", parent=BODY, fontSize=8.5, leading=11, textColor=colors.HexColor("#333333")
)


def rule_number(section: int, rule: int) -> str:
    """The number a policy rule is printed with: its section, then its place."""
    return f"{section}.{rule}"


def _check() -> None:
    """Refuse a policy whose identifiers repeat: each names one thing."""
    types = [section.policy_type for section in SECTIONS]
    if len(types) != len(set(types)):
        raise ValueError(f"a policy type is used twice: {types}")
    for section in SECTIONS:
        ids = [rule.id for rule in section.rules]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{section.policy_type} uses a rule id twice: {ids}")


def _flowables() -> List[Any]:
    parts: List[Any] = [
        Paragraph(TITLE_TEXT, TITLE),
        Spacer(1, 6),
        Paragraph(f"{ORGANISATION}. {POLICY_ID}.", SUBTITLE),
        Spacer(1, 16),
    ]
    for paragraph in INTRODUCTION:
        parts += [Paragraph(paragraph, BODY), Spacer(1, 8)]
    for s, section in enumerate(SECTIONS, 1):
        parts += [
            Spacer(1, 10),
            Paragraph(f"{s}. {section.heading} ({section.policy_type})", SECTION),
            Spacer(1, 6),
        ]
        for r, rule in enumerate(section.rules, 1):
            parts += [
                Paragraph(f"{rule_number(s, r)} {rule.heading} ({rule.id})", RULE),
                Paragraph(rule.text, BODY),
                Spacer(1, 8),
            ]
    parts += [Spacer(1, 14), Paragraph(CLOSING, SMALL)]
    return parts


def build(out_dir: Path) -> Path:
    """Write the policy PDF into ``out_dir`` and return its path."""
    _check()
    path = out_dir / POLICY_NAME
    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title=TITLE_TEXT,
        author=ORGANISATION,
        # A fixed creation date and document id: the same input gives the same file.
        invariant=1,
    )
    doc.build(_flowables())
    return path


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--out-dir",
        default="samples/rule-validation",
        help="Directory to write bank-statement-review-policy.pdf into",
    )
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = build(out_dir)
    rules = sum(len(section.rules) for section in SECTIONS)
    print(f"Wrote {path}: {len(SECTIONS)} policy types, {rules} policy rules")


if __name__ == "__main__":
    main()
