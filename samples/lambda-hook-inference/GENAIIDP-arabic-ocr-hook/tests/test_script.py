import pytest
from r5ocr.script import classify

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "text,expected",
    [
        ("جمهورية السودان", "ar"),
        ("Driving License", "en"),
        ("الذكاء الاصطناعي (Artificial Intelligence)", "en"),
        ("License Type : General نوع", "en"),
        ("0024991234567890", "en"),
        ("١٢٣٤٥", "ar"),
        ("", "none"),
        ("  - . ", "none"),
        ("ألزهايمـر", "ar"),
    ],
)
def test_classify(text, expected):
    assert classify(text) == expected
