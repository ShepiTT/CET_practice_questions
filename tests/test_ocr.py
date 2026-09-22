import os
import pytest

import ocr
import parser as paper_parser

MINI_PAPER = """
Part II Listening Comprehension
Questions 1 and 2 are based on the news report you have just heard.
1. A) It rained. C) It snowed.
B) It was windy. D) It was sunny.
2. A) One. B) Two. C) Three. D) Four.
"""


def test_lines_in_reading_order_groups_rows_and_sorts_left_to_right():
    box = lambda x, y: [[x, y], [x + 50, y], [x + 50, y + 10], [x, y + 10]]
    result = [
        (box(300, 100), 'C) It snowed.', 0.9),
        (box(10, 103), '1. A) It rained.', 0.9),   # same row, further left
        (box(10, 130), 'B) It was windy.', 0.9),   # next row
    ]
    assert ocr.lines_in_reading_order(result) == ['1. A) It rained. C) It snowed.', 'B) It was windy.']


def test_lines_in_reading_order_handles_empty():
    assert ocr.lines_in_reading_order(None) == []


def test_cache_path_mirrors_data_layout(tmp_path):
    import os
    assert ocr.cache_path(os.path.join(ocr.DATA_DIR, 'x.pdf')).endswith(os.path.join('ocr', 'x.txt'))
    assert ocr.cache_path(os.path.join(ocr.DATA_DIR, 'answers', 'x.pdf')).endswith(
        os.path.join('ocr', 'answers', 'x.txt'))


def test_scanned_pdf_uses_cached_ocr_text(monkeypatch):
    monkeypatch.setattr(paper_parser, 'pdf_text', lambda path: '')          # no text layer
    monkeypatch.setattr(ocr, 'cached_text', lambda path: MINI_PAPER)
    monkeypatch.setattr(ocr, 'ocr_pdf', lambda *a, **k: pytest.fail('must not OCR unless allowed'))
    groups = paper_parser.parse_cet_pdf_v2('scanned.pdf')
    assert [g['type'] for g in groups] == ['listening']
    q1 = groups[0]['questions'][0]
    assert [o['text'] for o in q1['options']] == ['It rained.', 'It was windy.', 'It snowed.', 'It was sunny.']


def test_scanned_pdf_without_cache_yields_nothing(monkeypatch):
    monkeypatch.setattr(paper_parser, 'pdf_text', lambda path: '')
    monkeypatch.setattr(ocr, 'cached_text', lambda path: None)
    assert paper_parser.parse_cet_pdf_v2('scanned.pdf') == []


def test_scanned_pdf_runs_ocr_when_allowed(monkeypatch):
    monkeypatch.setattr(paper_parser, 'pdf_text', lambda path: '')
    monkeypatch.setattr(ocr, 'cached_text', lambda path: None)
    monkeypatch.setattr(ocr, 'ocr_pdf', lambda path, **k: MINI_PAPER)
    assert len(paper_parser.parse_cet_pdf_v2('scanned.pdf', allow_ocr=True)) == 1


def test_text_layer_is_preferred_over_ocr(monkeypatch):
    monkeypatch.setattr(paper_parser, 'pdf_text', lambda path: MINI_PAPER * 20)
    monkeypatch.setattr(ocr, 'cached_text', lambda path: pytest.fail('text layer present'))
    assert paper_parser.parse_cet_pdf_v2('text.pdf')[0]['type'] == 'listening'


def test_normalize_ocr_repairs_digits_and_separators():
    raw = ("Questions I and 2 are based on the news report you have just heard.\n"
           "Questions 8 to ll are based on the conversation you have just heard.\n"
           "Questions 5l to 55 are based on the following passage.\n"
           "12, A) He wanted to order some wooden furniture.\n"
           "2l. A) Ship traffic.\n"
           "36. The number of people.\n"
           "5o. What does the author think?\n"
           "21: A) Spreading news.\n"
           "I like it. 12, 000 people.\n")
    out = paper_parser.normalize_ocr(raw)
    assert "Questions 1 and 2 are" in out
    assert "Questions 8 to 11 are" in out
    assert "Questions 51 to 55 are" in out
    assert "12. A) He wanted" in out
    assert "21. A) Ship" in out
    assert "36. The number" in out
    assert "50. What does" in out
    assert "21. A) Spreading" in out
    assert "I like it. 12, 000 people." in out   # prose is left alone


def test_word_bank_claims_bare_token_for_missing_label():
    raw = 'A) appearance C) avoid H incredibly M) statement D) considerable D normal N) tend'
    bank = {o['label']: o['text'] for o in paper_parser.extract_word_bank(raw)}
    assert bank['H'] == 'incredibly'
    assert bank['I'] == 'normal'
    assert bank['D'] == 'considerable'


def test_word_bank_single_missing_label_claims_any_bare_token():
    raw = 'A) a B) b C) c D) d E) e F) f G) g H) h I) i J) j K) k L) l M) m N) n B oscar'
    bank = {o['label']: o['text'] for o in paper_parser.extract_word_bank(raw)}
    assert bank['O'] == 'oscar' and bank['B'] == 'b'


@pytest.mark.parametrize('raw, label, word', [
    ('A)associated F)imitate K)principal B)coincidence G)indication L)recognizable C)determined '
     'H)integrate M simply D)drastically Dmaximizes N)stressful E)enormous Dnatural O)symbolizes', 'I', 'maximizes'),
    ('A)associated F)imitate K)principal B)coincidence G)indication L)recognizable C)determined '
     'H)integrate M simply D)drastically Dmaximizes N)stressful E)enormous Dnatural O)symbolizes', 'J', 'natural'),
    ('A) constantly F) load K)removed B)credible G)miserable L) stacks C) essential Hpressure M) suspicion '
     'D) exploring ) properly N) tracked E) gather J)records 0) watching', 'H', 'pressure'),
    ('A) constantly F) load K)removed B)credible G)miserable L) stacks C) essential Hpressure M) suspicion '
     'D) exploring ) properly N) tracked E) gather J)records 0) watching', 'I', 'properly'),
])
def test_word_bank_recovers_ocr_bank_glitches(raw, label, word):
    bank = {o['label']: o['text'] for o in paper_parser.extract_word_bank(raw)}
    assert len(bank) == 15
    assert bank[label] == word


# ---------------------------------------------------------------- usable text layers

class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _FakePdf:
    def __init__(self, pages):
        self.pages = [_FakePage(t) for t in pages]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_pdf(monkeypatch, *page_texts):
    monkeypatch.setattr(ocr.pdfplumber, 'open', lambda path: _FakePdf(page_texts))


ANSWER_KEY = os.path.join(ocr.DATA_DIR, 'answers', 'x.pdf')
EXAM_PAPER = os.path.join(ocr.DATA_DIR, 'x.pdf')


def test_answer_key_with_mojibake_text_layer_needs_ocr(monkeypatch):
    # 50k characters, but the custom font encoding leaves almost no Chinese behind
    _fake_pdf(monkeypatch, 'ffi ffilf3i5ll:l:lmlm !IHJl:1g' * 2000 + '答案详解')
    assert ocr.has_text_layer(ANSWER_KEY) is False


def test_answer_key_with_real_chinese_keeps_its_text_layer(monkeypatch):
    _fake_pdf(monkeypatch, '答案详解 本题考查动词辨析，由此确定答案为L。' * 60)
    assert ocr.has_text_layer(ANSWER_KEY) is True


def test_exam_paper_is_judged_on_length_alone(monkeypatch):
    # papers are English: a text layer with no Chinese at all is perfectly normal
    _fake_pdf(monkeypatch, 'Questions 1 and 2 are based on the news report you have just heard. ' * 40)
    assert ocr.has_text_layer(EXAM_PAPER) is True


def test_short_text_layer_is_not_enough(monkeypatch):
    _fake_pdf(monkeypatch, 'a page number and nothing else')
    assert ocr.has_text_layer(EXAM_PAPER) is False
    assert ocr.has_text_layer(ANSWER_KEY) is False


def test_is_answer_key_only_for_the_answers_folder():
    assert ocr.is_answer_key(ANSWER_KEY) is True
    assert ocr.is_answer_key(EXAM_PAPER) is False
    assert ocr.is_two_column(ANSWER_KEY) is True
