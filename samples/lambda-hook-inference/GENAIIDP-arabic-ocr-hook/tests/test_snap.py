import pytest
from r5ocr.snap import snap_to_transcription

pytestmark = pytest.mark.unit

PAGE = "رخصة قيادة\nالعنوان: شارع الياسمين ١٢، حي النخيل، مدينة الواحة\nSerial No. UTA 7712 0045"


def test_restores_dropped_arabic_indic_digits():
    paddle = "شارع الياسمين ، حي النخيل، مدينة الواحة"
    assert (
        snap_to_transcription(paddle, PAGE)
        == "شارع الياسمين ١٢، حي النخيل، مدينة الواحة"
    )


def test_restores_a_misread_digit():
    # PaddleOCR read "١٢" as "1": digits are ignored when matching, so the
    # transcription's digits win.
    paddle = "شارع الياسمين 1، حي النخيل، مدينة الواحة"
    assert (
        snap_to_transcription(paddle, PAGE)
        == "شارع الياسمين ١٢، حي النخيل، مدينة الواحة"
    )


def test_does_not_pull_in_the_label_outside_the_span():
    assert not snap_to_transcription("شارع الياسمين حي", PAGE).startswith("العنوان")


def test_keeps_text_when_a_letter_differs():
    paddle = "شارع الياسمبن ، حي النخيل"  # one letter misread
    assert snap_to_transcription(paddle, PAGE) == paddle


def test_keeps_text_when_the_reading_is_ambiguous():
    page = "مدينة الواحة ١\nمدينة الواحة ٢"
    assert snap_to_transcription("مدينة الواحة", page) == "مدينة الواحة"


def test_keeps_short_text():
    assert snap_to_transcription("حي", PAGE) == "حي"


def test_does_not_match_across_lines():
    page = "شارع الياسمين\nحي النخيل"
    assert snap_to_transcription("شارع الياسمين حي النخيل", page) == (
        "شارع الياسمين حي النخيل"
    )


def test_empty_transcription_keeps_text():
    assert snap_to_transcription("شارع الياسمين", "") == "شارع الياسمين"
