# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""NUMERIC_EXACT on amounts written in non-ASCII digits.

Documents in Arabic, Persian, Hindi and other scripts write amounts in their own
digits. Before stickler-eval picked up awslabs/stickler#560, its
``NumericComparator`` deleted those digits, so an extracted ``"١٬٢٥٠٫٠٠"`` never
matched an expected ``"1,250.00"``, and a value that mixed scripts
(``"١٢٥٠.50"``) could match the wrong number. These tests run the real
``EvaluationService`` and Stickler, without mocking the comparison.

The fields are declared ``"type": "string"``: a ``number`` field is converted
with ``float()`` before Stickler sees it, which reads plain non-ASCII digits but
not their separators.
"""

import pytest

from idp_common.evaluation.service import EvaluationService
from idp_common.models import Section

# expected (ground truth), extracted, whether they should match
CASES = {
    "arabic_total": ("1,250.00", "١٬٢٥٠٫٠٠", True),
    "aed_currency": ("1250.50", "\u200f١٬٢٥٠٫٥٠\u00a0د.إ.\u200f", True),
    "persian": ("1250.5", "۱٬۲۵۰٫۵", True),
    "persian_negative": ("-1250.5", "\u200e−۱٬۲۵۰٫۵", True),
    "hindi_lakh": ("1234567.89", "१२,३४,५६७.८९", True),
    "fullwidth": ("1250.50", "１２５０．５０", True),
    "plain_arabic": ("1250", "١٢٥٠", True),
    "bilingual": ("1250.00", "1,250.00 (١٬٢٥٠٫٠٠)", True),
    "different_amount": ("1250", "١٢٥١", False),
    "mixed_script_misread": ("0.5", "١٢٥٠.50", False),
    "persian_date": ("1402", "۱۴۰۲/۰۵/۱۴", False),
}


# The same amounts in a field declared "type": "number". IDP converts these with
# float() before Stickler sees them, which reads bare non-ASCII digits but not
# their separators, so this path is unchanged by the Stickler upgrade.
NUMBER_CASES = {
    "number_bare_arabic": ("1250", "١٢٥٠", True),
    "number_arabic_separators": ("1250", "١٬٢٥٠٫٠٠", False),
    "number_fullwidth_point": ("1250.5", "１２５０．５", False),
    "number_ascii": ("1250", "1250.00", True),
}


def _config():
    properties = {
        name: {"type": "string", "x-aws-idp-evaluation-method": "NUMERIC_EXACT"}
        for name in CASES
    }
    properties.update(
        {
            name: {"type": "number", "x-aws-idp-evaluation-method": "NUMERIC_EXACT"}
            for name in NUMBER_CASES
        }
    )
    return {
        "classes": [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "invoice",
                "x-aws-idp-document-type": "Invoice",
                "type": "object",
                "properties": properties,
            }
        ]
    }


@pytest.mark.unit
class TestNonAsciiDigitAmounts:
    @classmethod
    def setup_class(cls):
        service = EvaluationService(region="us-east-1", config=_config(), max_workers=1)
        section = Section(
            section_id="1",
            classification="Invoice",
            page_ids=["1"],
            confidence=1.0,
        )
        result = service.evaluate_section(
            section=section,
            expected_results={
                name: case[0] for name, case in {**CASES, **NUMBER_CASES}.items()
            },
            actual_results={
                name: case[1] for name, case in {**CASES, **NUMBER_CASES}.items()
            },
        )
        cls.attributes = {attribute.name: attribute for attribute in result.attributes}

    @pytest.mark.parametrize("name", sorted(CASES))
    def test_amount_matches_only_when_it_is_the_same_number(self, name):
        expected, extracted, should_match = CASES[name]
        attribute = self.attributes[name]
        assert attribute.matched is should_match, (expected, extracted)
        assert attribute.score == (1.0 if should_match else 0.0)

    def test_every_field_was_compared_with_numeric_exact(self):
        """The result comes from Stickler's NumericComparator, which the service
        reports as ``NumericExact``, not from a fallback comparison."""
        assert set(self.attributes) == set(CASES) | set(NUMBER_CASES)
        for attribute in self.attributes.values():
            assert attribute.evaluation_method == "NumericExact", attribute.name

    @pytest.mark.parametrize("name", sorted(NUMBER_CASES))
    def test_a_number_field_reads_amounts_through_float(self, name):
        expected, extracted, should_match = NUMBER_CASES[name]
        attribute = self.attributes[name]
        assert attribute.matched is should_match, (expected, extracted)
        assert attribute.score == (1.0 if should_match else 0.0)
