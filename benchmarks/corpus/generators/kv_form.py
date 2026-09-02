#!/usr/bin/env python3
"""Synthetic flat key/value FORM generator with exact ground truth.

A non-list document type (breadth): N labeled scalar fields, exact known values.
build(fields, out) -> <out>.pdf + <out>.truth.json

Two ground truths, deliberately:

* ``fields``       — the text the document RENDERS (``"$685.50"``, ``"3.90%"``,
  ``"05/14/1985"``). Scored by ``scalar_accuracy``.
* ``fields_typed`` — what a correctly-typed extraction must produce under a
  schema that declares these fields ``number`` / ``format: date`` (``685.5``,
  ``3.9``, ``"1985-05-14"``). Scored by ``typed_accuracy``.

Keeping both is the point. A truth file that recorded only the rendered text
would score a pipeline that correctly returns the *number* as WRONG — so any
value-normalization behaviour would be measured as a regression, which is
precisely backwards. Only the fields a typed schema would actually convert appear
in ``fields_typed``; ``Policy Number``, ``ZIP Code``, ``Tax ID`` and friends stay
strings on purpose (a ZIP is not a number, and coercing it would be a bug).
"""

import json

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

LABELS = [
    "Full Name",
    "Date of Birth",
    "Policy Number",
    "Effective Date",
    "Premium",
    "Deductible",
    "Coverage Limit",
    "Agent Name",
    "Agent ID",
    "Phone",
    "Email",
    "Street Address",
    "City",
    "State",
    "ZIP Code",
    "Country",
    "Account Number",
    "Routing Number",
    "Balance",
    "Interest Rate",
    "Employer",
    "Occupation",
    "Annual Income",
    "Tax ID",
    "Reference Number",
]


def _value(label, i):
    v = {
        "Full Name": "Jane Q Public",
        "Date of Birth": "05/14/1985",
        "Policy Number": f"POL-{100000 + i}",
        "Effective Date": "01/01/2024",
        "Premium": f"${(i + 1) * 137}.50",
        "Deductible": f"${(i + 1) * 100}",
        "Coverage Limit": f"${(i + 1) * 10000}",
        "Agent Name": "John A Broker",
        "Agent ID": f"AG{2000 + i}",
        "Phone": f"555-0{100 + i}",
        "Email": "jane.public@example.com",
        "Street Address": f"{100 + i} Main Street",
        "City": "Anytown",
        "State": "CA",
        "ZIP Code": f"9021{i % 10}",
        "Country": "USA",
        "Account Number": f"{500000000 + i}",
        "Routing Number": "021000021",
        "Balance": f"${(i + 1) * 2500}.00",
        "Interest Rate": f"{2 + i * 0.1:.2f}%",
        "Employer": "AnyCompany Inc",
        "Occupation": "Engineer",
        "Annual Income": f"${80000 + i * 1000}",
        "Tax ID": f"12-345{6000 + i}",
        "Reference Number": f"REF{9000 + i}",
    }
    return v.get(label, f"value-{i}")


# Fields a typed schema converts, and the converter to apply to the rendered text
# to get the typed expectation. Anything absent from this map stays a string.
def _typed(label, rendered):
    """The schema-typed expectation for ``rendered``, or None if it stays a string."""
    if label in ("Premium", "Deductible", "Coverage Limit", "Balance", "Annual Income"):
        # "$1,234.50" -> 1234.5
        return float(rendered.replace("$", "").replace(",", ""))
    if label == "Interest Rate":
        # "3.90%" -> 3.9. Magnitude preserved, NOT divided by 100 — matching the
        # pipeline's documented percent handling.
        return float(rendered.rstrip("%"))
    if label in ("Date of Birth", "Effective Date"):
        # "05/14/1985" -> "1985-05-14" (ISO-8601, what `format: date` means).
        mm, dd, yyyy = rendered.split("/")
        return f"{yyyy}-{mm}-{dd}"
    return None


def _class_schema(labels):
    """The document class this generator's output should be extracted under.

    Emitted alongside the PDF, and derived from the SAME ``_typed`` map that
    builds ``fields_typed``, so the schema and the ground truth cannot drift.
    That matters more than it sounds: the corpus previously had no class for this
    generator at all (``doc_matrix.yaml`` excluded it as "future work"), so it was
    either unscored or scored under an unrelated forms schema — which pins
    accuracy to a meaningless value and looks like a real number.

    Evaluation methods mirror what the type implies: NUMERIC_EXACT for numbers,
    DATE for dates (format-tolerant, so the stack's own evaluation does not
    penalize a correctly normalized value), EXACT for identifiers, FUZZY for
    free text.
    """
    fuzzy = {
        "Full Name",
        "Agent Name",
        "Street Address",
        "City",
        "Employer",
        "Occupation",
    }
    props = {}
    for label in labels:
        typed = _typed(label, _value(label, 0))
        if isinstance(typed, float):
            props[label] = {
                "type": "number",
                "description": f"{label} as a number, without currency symbol or thousands separators.",
                "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
            }
        elif isinstance(typed, str):
            props[label] = {
                "type": "string",
                "format": "date",
                "description": f"{label} in ISO-8601 format (YYYY-MM-DD).",
                "x-aws-idp-evaluation-method": "DATE",
            }
        else:
            props[label] = {
                "type": "string",
                "description": f"{label} exactly as printed on the form.",
                "x-aws-idp-evaluation-method": "FUZZY" if label in fuzzy else "EXACT",
            }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "Policy Application Form",
        "type": "object",
        "x-aws-idp-document-type": "Policy Application Form",
        "description": (
            "A flat policy application form: labelled scalar fields covering the "
            "applicant, the policy amounts, the agent, and the bank account. No "
            "tables or repeating sections."
        ),
        "properties": props,
    }


def build(fields=25, out="kv.pdf"):
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(out, pagesize=letter)
    story = [
        Paragraph("<b>Policy Application Form</b>", styles["Title"]),
        Spacer(1, 0.2 * inch),
    ]
    gt, gt_typed = {}, {}
    for i in range(min(fields, len(LABELS))):
        label = LABELS[i]
        val = _value(label, i)
        gt[label] = val
        typed = _typed(label, val)
        if typed is not None:
            gt_typed[label] = typed
        story.append(Paragraph(f"<b>{label}:</b> {val}", styles["Normal"]))
        story.append(Spacer(1, 0.08 * inch))
    doc.build(story)
    truth = {
        "gen": "kv_form",
        "fields": gt,
        "fields_typed": gt_typed,
        "seq_ids": [],
        "per_list": None,
        "list_key": None,
    }
    json.dump(truth, open(out + ".truth.json", "w"))
    # The matching document class, so this generator's output is scored under a
    # schema that actually declares its field types. make_configs.py reads it.
    import yaml as _yaml

    with open(out + ".classes.yaml", "w") as fh:
        _yaml.safe_dump(
            {
                "notes": "Benchmark class for the kv_form generator (auto-emitted).",
                "use_bda": False,
                "classes": [_class_schema(list(gt))],
            },
            fh,
            sort_keys=False,
        )
    return {"out": out, "fields": len(gt)}
