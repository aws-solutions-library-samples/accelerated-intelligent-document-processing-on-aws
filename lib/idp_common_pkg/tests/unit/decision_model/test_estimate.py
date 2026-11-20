# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``estimate_tokens`` against what the deployed decider measured (#2051).

The live window check (``make stacktest-decision-window``) sent extraction
confidence requests cut to the default ``max_input_tokens`` of 3584, and the
decider refused three page shapes because the estimate of their text was about 20%
low: a markdown statement table and 17-character serials. The fixtures below are
those pages, their refused prefixes, and the counts the decider reported for them.
The counts for the text the new estimate keeps were measured with the pinned
decider model's own tokenizer, which reproduces the decider's reported state
counts for these pages to within the decider's fixed prompt of about 8 tokens.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from idp_common.decision_model import (
    DEFAULT_MAX_INPUT_TOKENS,
    estimate_tokens,
    fit_text,
    state_budget,
    truncate_to_tokens,
)
from idp_common.decision_model.wire import CONTEXT_WINDOW_TOKENS

pytestmark = pytest.mark.unit

#: The characters of the serials page, as the live check generates them.
SERIAL_ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"


def _serial(seed: int) -> str:
    out = []
    for _ in range(17):
        seed = (seed * 1103515245 + 12345) % 2147483648
        out.append(SERIAL_ALPHABET[(seed >> 16) % len(SERIAL_ALPHABET)])
    return "".join(out)


def _page(style: str) -> str:
    """Page 1 of a live check style (``dense_page`` in the check's script)."""
    i_range = range(150)
    if style == "table":
        rows = ["| Date | Amount | Balance |", "|---|---|---|"]
        rows += [
            f"| {i % 28 + 1:02d}/11/2026 | {i * 7919 % 100000:,}.{i % 100:02d} | "
            f"{(i + 1) * 9973 % 1000000:,}.{i * 7 % 100:02d} |"
            for i in i_range
        ]
        return "\n".join(rows)
    if style == "codes":
        return "\n".join(_serial(7919 + i) for i in range(400))
    if style == "figures":
        return "\n".join(
            f"01-{i:05d} 2026-11-{i % 28 + 1:02d} "
            f"{i * 7919 % 100000:>9,}.{i % 100:02d} | {i * 31 % 997:03d}"
            for i in i_range
        )
    if style == "words":
        return "\n".join(
            f"Page 1 line {i}: " + "statement carried forward balance " * 12
            for i in range(24)
        )
    raise ValueError(style)


#: The first 16 hex digits of each page's SHA-256, so a change to a generator
#: above cannot leave the measured counts describing different text.
PAGE_SHA256 = {
    "table": "119a15aaa02078af",
    "codes": "411647d7dd548051",
    "figures": "50a95ef23e4cae5a",
    "words": "664bdeb40b5cdf3b",
}

#: The estimate of the longest confidence question the live check asked
#: (``account_number``'s), which every one of its confidence requests reserved.
QUESTION_ESTIMATE = 122.0

#: Each live confidence request the decider refused at 3584, as
#: ``(style, images, characters of state sent, state tokens and total tokens in
#: the refusal message, characters the new estimate keeps, tokenizer count of
#: those characters)``. The total less the state is what the request spends
#: outside its text (question, images, the decider's fixed prompt).
LIVE_REFUSALS = [
    ("table", 0, 4402, 4054, 4170, 3774, 3466),
    ("codes", 0, 5400, 4188, 4304, 4284, 3324),
    ("codes", 1, 5004, 3888, 4200, 3960, 3078),
]


@pytest.mark.parametrize("style", sorted(PAGE_SHA256))
def test_the_fixture_pages_are_the_text_the_decider_counted(style):
    digest = hashlib.sha256(_page(style).encode()).hexdigest()[:16]
    assert digest == PAGE_SHA256[style]


@pytest.mark.parametrize(
    "style, images, sent, state, total, _kept, _tokens", LIVE_REFUSALS
)
def test_each_refused_state_is_estimated_within_3_percent_of_the_decider_count(
    style, images, sent, state, total, _kept, _tokens
):
    """The decider counted this text; the estimate is no more than 3% below it.

    The estimate this replaces put these at 3447 to 3466 against 3888 to 4188.
    """
    assert estimate_tokens(_page(style)[:sent]) >= 0.97 * state


@pytest.mark.parametrize(
    "style, images, sent, state, total, kept, tokens", LIVE_REFUSALS
)
def test_each_refused_request_is_now_cut_to_fit_the_window(
    style, images, sent, state, total, kept, tokens
):
    budget = state_budget(DEFAULT_MAX_INPUT_TOKENS, QUESTION_ESTIMATE, images)
    text = fit_text(_page(style), budget, f"page 1 ({style})")
    assert len(text) < sent
    # The measured counts below are for exactly this cut; a change to the weights
    # moves it, and the tokenizer counts then have to be measured again.
    assert len(text) == kept
    assert tokens + (total - state) <= CONTEXT_WINDOW_TOKENS


def _previous_estimate(text: str) -> float:
    """The flat estimate #2149 shipped: digits and non-ASCII one, the rest half."""
    digits = sum(1 for c in text if "0" <= c <= "9")
    non_ascii = sum(1 for c in text if ord(c) > 127)
    return digits + non_ascii + (len(text) - digits - non_ascii) / 2


PROSE = (
    "The account holder agrees that the statement balance carried forward "
    "reflects all transactions posted during the period. Payments received after "
    "the closing date will appear on the next statement.\n"
) * 50


def _longest_prefix(text: str, budget: float, estimate) -> int:
    """How many leading characters of ``text`` ``estimate`` fits in ``budget``."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return low


@pytest.mark.parametrize("text", [_page("words"), PROSE], ids=["words", "prose"])
def test_prose_is_estimated_and_cut_within_2_percent_of_the_previous_estimate(text):
    """Prose fitted before and still fits: only digit and punctuation text moves."""
    assert estimate_tokens(text) <= 1.02 * _previous_estimate(text)
    budget = state_budget(DEFAULT_MAX_INPUT_TOKENS, QUESTION_ESTIMATE, 0)
    previous = _longest_prefix(text, budget, _previous_estimate)
    assert previous < len(text)
    assert _longest_prefix(text, budget, estimate_tokens) >= 0.98 * previous


@pytest.mark.parametrize(
    "text, tokens",
    [
        ("7", 1.0),
        ("|", 1.0),
        (",.", 2.0),
        ("ab", 1.0),
        ("AB", 1.0),
        ("A1B", 2.5),
        ("1HGCM", 4.0),
        (" \n", 1.0),
        ("日本", 2.0),
        ("ab-12", 4.0),
        # Letter-only runs that read as codes count like letters beside digits.
        ("kQzRbT", 4.5),
        ("XKQZPT", 4.5),
        ("bcdfgh", 4.5),
        ("BCDFGA", 4.5),
        ("Statement", 4.5),
        ("INVOICE", 3.5),
        # Ideographs by level: GB 2312 or JIS X 0208 first level, Big5 first
        # level only, any other.
        ("们", 1.0),
        ("請", 1.0),
        ("們", 1.5),
        ("禤", 2.5),
        ("𠀀", 4.0),
        # The ends of each level: Big5's last first-level character and its first
        # second-level one, GB 2312's first and last first-level and first
        # second-level, JIS X 0208's first first-level and first second-level.
        ("籲", 1.5),
        ("乂", 2.5),
        ("啊", 1.0),
        ("座", 1.0),
        ("亜", 1.0),
        ("椀", 1.0),  # JIS X 0208's last first-level row, in no other set
        # The ideograph blocks' ends: extension A, compatibility ideographs, the
        # supplementary blocks (extension B's last, G's first, the last of all),
        # and the first code point after them.
        ("\u3400", 3.0),
        ("\u4dbf", 3.0),
        ("\ufa11", 2.5),
        ("\U0002a6df", 4.0),
        ("\U00030000", 4.0),
        ("\U000323af", 4.0),
        ("\U000323b0", 1.0),
        ("亍", 2.5),
        ("弌", 2.5),
        ("の", 1.0),
        ("한", 1.0),
    ],
)
def test_each_character_class_is_weighted(text, tokens):
    assert estimate_tokens(text) == tokens


#: Words of statement and form text, which must still count as words.
STATEMENT_WORDS = (
    "Statement Balance CARRIED FORWARD Interest Withdrawals Payments Deposit "
    "InvoiceNumber AccountHolder iPhone McDonald USA LLC IRS Mr Dr pH "
    "Schedule"
).split()


@pytest.mark.parametrize("word", STATEMENT_WORDS)
def test_ordinary_words_count_as_words(word):
    from idp_common.decision_model.budget import WORD_LETTER_TOKENS

    assert estimate_tokens(word) == WORD_LETTER_TOKENS * len(word)


@pytest.mark.parametrize(
    "code",
    ["kQzRbTmWpL", "aGVsbGgd", "XKQZPTRBNM", "qwrtzpxk", "BCDFGHAB", "zzzzza"],
)
def test_random_letter_codes_count_as_codes(code):
    from idp_common.decision_model.budget import CODE_RUN_LETTER_TOKENS

    assert estimate_tokens(code) == CODE_RUN_LETTER_TOKENS * len(code)


@pytest.mark.parametrize("style", ["table", "codes", "figures", "words"])
@pytest.mark.parametrize("budget", [50.0, 777.5, 2000.0])
def test_a_cut_never_holds_more_than_its_budget(style, budget):
    kept, dropped = truncate_to_tokens(_page(style), budget)
    assert dropped > 0
    assert estimate_tokens(kept) <= budget


# --- Letter-only codes and rare CJK characters (#2212) -------------------------


def _letters(seed: int, length: int, alphabet: str) -> str:
    out = []
    for _ in range(length):
        seed = (seed * 1103515245 + 12345) % 2147483648
        out.append(alphabet[(seed >> 16) % len(alphabet)])
    return "".join(out)


_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _gb2312_level_two(count: int) -> str:
    """``count`` characters of GB 2312's second level, spread over its rows."""
    return "".join(
        bytes((0xD8 + k * 7 % 32, 0xA1 + k * 13 % 94)).decode("gb2312")
        for k in range(count)
    )


#: Shapes the estimate before #2212 weighed as words or common CJK, which the
#: decider's tokenizer splits into one to three tokens a character.
CODE_AND_RARE_SHAPES = {
    "mixed-case codes": lambda: "\n".join(
        _letters(11 + i, 12, _LETTERS) for i in range(60)
    ),
    "mixed-case words": lambda: " ".join(
        _letters(i, 3 + i % 6, _LETTERS) for i in range(150)
    ),
    "base64 letters": lambda: "\n".join(
        _letters(29 + i, 64, _LETTERS + "+/") for i in range(12)
    ),
    "capital codes": lambda: " ".join(
        _letters(41 + i, 10, _LETTERS[:26]) for i in range(70)
    ),
    "lower-case codes": lambda: " ".join(
        _letters(53 + i, 10, _LETTERS[26:]) for i in range(70)
    ),
    "rare hanzi": lambda: _gb2312_level_two(400),
    "rare surnames": lambda: "\n".join(
        f"{i + 1}. 姓名 "
        + "禤亓厍逯鬲昝隗郗仉笪郇毌"[i % 12]
        + "珺玥煜堃喆赟昶炜琰祎"[i % 10]
        for i in range(60)
    ),
    "rare name kanji": lambda: "\n".join(
        f"氏名 {'髙﨑彅遙凜雫苺颯椿柊皐澪琥珀瑛綾楓梓櫂'[i % 19]}"
        f"{'彧翀骞晞犇淼焱垚'[i % 8]}"
        for i in range(60)
    ),
}

#: ``(first 16 hex digits of the shape's SHA-256, its count by the decider's
#: tokenizer)``, measured with the pinned model's tokenizer (#2212).
CODE_AND_RARE_TOKENS = {
    "mixed-case codes": ("d47f78975b64446f", 530),
    "mixed-case words": ("dfa839390f511a54", 561),
    "base64 letters": ("a26d99722d613405", 521),
    "capital codes": ("997356291c23adc9", 424),
    "lower-case codes": ("075df464bc749e0a", 375),
    "rare hanzi": ("a552048adaf44bbb", 725),
    "rare surnames": ("51676ecfd1a01d3b", 632),
    "rare name kanji": ("162a156e397363bd", 451),
}

#: What a confidence request with text only spent outside its text in the live
#: check: question, the decider's prompt (#2051, run 4: total less the tokenizer
#: count of the state, 124 for every page shape). Its question was estimated at
#: ``QUESTION_ESTIMATE``, so its text was cut to the default less that.
CONFIDENCE_TEXT_OVERHEAD_TOKENS = 124


@pytest.mark.parametrize("shape", sorted(CODE_AND_RARE_SHAPES))
def test_the_code_and_rare_shapes_are_the_text_the_tokenizer_counted(shape):
    digest = hashlib.sha256(CODE_AND_RARE_SHAPES[shape]().encode()).hexdigest()
    assert digest[:16] == CODE_AND_RARE_TOKENS[shape][0]


@pytest.mark.parametrize("shape", sorted(CODE_AND_RARE_SHAPES))
def test_text_of_each_code_and_rare_shape_cut_to_the_default_fits_the_window(shape):
    """A confidence request whose text is all this shape is predicted to fit.

    The text is cut to the default ``max_input_tokens`` less the question, and
    costs that budget times this shape's real tokens per estimated token. Before
    #2212 the ratio was 1.15 to 1.81 for six of these shapes (the window allows
    about 1.15), so a request of mixed-case codes, base64 letters or rare CJK
    characters overflowed; it is now 0.77 to 1.08.
    """
    text = CODE_AND_RARE_SHAPES[shape]()
    ratio = CODE_AND_RARE_TOKENS[shape][1] / estimate_tokens(text)
    budget = state_budget(DEFAULT_MAX_INPUT_TOKENS, QUESTION_ESTIMATE, 0)
    assert ratio * budget + CONFIDENCE_TEXT_OVERHEAD_TOKENS <= CONTEXT_WINDOW_TOKENS
    assert ratio <= 1.1


def _estimate_2209(text: str) -> float:
    """The per-character-class estimate #2209 shipped, before #2212."""
    total = 0.0
    position = 0
    for run in re.finditer(r"[A-Za-z0-9]+", text):
        total += sum(
            0.5 if c in " \t\n\r" else 1.0 for c in text[position : run.start()]
        )
        digits = sum(c.isdigit() for c in run.group())
        letter = 0.75 if digits else 0.5
        total += digits + letter * (len(run.group()) - digits)
        position = run.end()
    return total + sum(0.5 if c in " \t\n\r" else 1.0 for c in text[position:])


ZH_HANS = (
    "尊敬的客户：感谢您选择我行的服务。本对账单列示了您的账户在本期内的全部交易明细，"
    "包括存款、取款、转账及各项费用。请您仔细核对账单中的各项内容，如对任何交易有疑问，"
    "请在收到对账单之日起三十日内与我们联系，逾期视为您已确认账单内容无误。\n"
    "本合同自双方签字盖章之日起生效，有效期为一年。合同期满前三十日内，如任何一方未提出"
    "书面异议，本合同自动续期一年。因履行本合同发生的争议，双方应友好协商解决。\n"
)
ZH_HANT = (
    "親愛的客戶您好：感謝您選擇本行的服務。本對帳單列示您的帳戶於本期內的全部交易明細，"
    "包括存款、提款、轉帳及各項費用。請您詳細核對帳單內容，如對任何交易有疑問，"
    "請於收到對帳單之日起三十日內與我們聯繫，逾期視為您已確認帳單內容無誤。\n"
    "本契約自雙方簽名蓋章之日起生效，有效期間為一年。契約期滿前三十日內，如任何一方未以"
    "書面提出異議，本契約自動延長一年。因履行本契約所生之爭議，雙方應本誠信原則協商解決。\n"
)
JA = (
    "お客様各位：平素より当行をご利用いただき、誠にありがとうございます。本明細書には、"
    "当期におけるお客様の口座のお取引内容をすべて記載しております。ご不明な点がございま"
    "したら、明細書到着後三十日以内に当行までお問い合わせください。\n"
    "本契約は、双方が署名捺印した日から効力を生じ、有効期間は一年間とします。本契約に関"
    "して紛争が生じた場合は、東京地方裁判所を第一審の専属的合意管轄裁判所とします。\n"
)
JA_NAMES = (
    "氏名：髙橋 雄一郎　住所：東京都世田谷区桜新町二丁目十五番八号\n"
    "氏名：渡邊 美咲　住所：大阪府大阪市北区梅田一丁目三番一号　勤務先：株式会社山﨑製作所\n"
    "氏名：齋藤 健太　住所：神奈川県横浜市中区山下町十番地　生年月日：平成元年四月一日\n"
)
#: Realistic text and the most #2212 may raise its estimate over #2209's, so
#: everyday text keeps nearly all the room it had. Measured: English prose (this
#: repository's documentation, code blocks dropped) +0.5%, simplified Chinese and
#: Japanese prose 0%, traditional Chinese prose +6.5% here and up to +12% on
#: denser bank wording (common traditional characters outside GB 2312 count
#: 1.5), Japanese names with rare kanji +2.4% here and up to +7%.
PROSE_MARGINS = [
    (PROSE, 1.0),
    (ZH_HANS, 1.0),
    (JA, 1.0),
    (ZH_HANT, 1.07),
    (JA_NAMES, 1.05),
]


@pytest.mark.parametrize(
    "text, margin", PROSE_MARGINS, ids=["en", "zh-hans", "ja", "zh-hant", "ja-names"]
)
def test_everyday_text_is_estimated_within_its_margin_of_the_2209_estimate(
    text, margin
):
    assert (
        _estimate_2209(text) <= estimate_tokens(text) <= margin * _estimate_2209(text)
    )


@pytest.mark.parametrize(
    "run, code",
    [
        ("aBeCiDoF", True),  # case flips only
        ("aBaB", True),  # four letters, the fewest the case sign reads
        ("aBa", False),
        ("aBcdefG", True),  # two flips in seven: exactly once every three
        ("aBcdeFghi", True),  # three flips in nine letters: once every three
        ("aBcdeFghijk", False),  # three in eleven
        ("astrnge", True),  # five consonants in a row
        ("abstract", False),  # four
        ("bcdfaghjakl", True),  # two vowels in eleven letters, under a fifth
        ("bcdfaghjak", False),  # two in ten
        ("bcdafg", True),  # one in six, the fewest letters the vowel sign reads
        ("dryly", False),  # ``y`` is a vowel
    ],
)
def test_each_sign_of_a_code_has_its_stated_threshold(run, code):
    from idp_common.decision_model.budget import _reads_as_code

    assert _reads_as_code(run) is code
