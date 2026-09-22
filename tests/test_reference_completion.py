import sqlite3
from lxml import html

from fetch_reference_keys import passage_prefix_matches, validate_question
from align_listening import align


def test_passage_prefix_accepts_ocr_punctuation_but_not_other_passage():
    script = 'It was perhaps when my parents who also happen to be my housemates left to go traveling for a couple of months recently that it dawned on me.'
    assert passage_prefix_matches(script, 'Directions. ' + script.replace('traveling', 'travelling'))
    assert not passage_prefix_matches(script, 'An entirely unrelated reading passage. ' * 20)


def test_options_must_match_not_just_question_number():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE option(question_id INTEGER,label TEXT,text TEXT)')
    conn.executemany('INSERT INTO option VALUES(1,?,?)', [('A','A first choice'),('B','A second choice'),('C','A third choice'),('D','A fourth choice')])
    node = html.fromstring('<div id="qp-1">' + ''.join(f'<span data-pdh-letter="{label}">{text}</span>' for label,text in [('A','A first choice'),('B','A second choice'),('C','A third choice'),('D','A fourth choice')]) + '</div>')
    question = {'id':1,'q_number':1,'group_type':'listening','content':'Question 1'}
    assert validate_question(conn, question, node, 'B') == 1
    node.xpath('//*[@data-pdh-letter="B"]')[0].text = 'A different test version'
    assert validate_question(conn, question, node, 'B') is None


SCRIPT = 'Tonight we have a very special guest who has recently published a fascinating new book about the history of science and technology in our modern world'


def timed_words(text, start=20):
    return [{'word': word, 'start': start+i*.5, 'end':start+i*.5+.4} for i,word in enumerate(text.split())]


def test_alignment_uses_local_word_times():
    result = align(SCRIPT, timed_words(SCRIPT, 48))
    assert result['start'] == 47.35
    assert result['score'] == 1


def test_alignment_rejects_repeated_ambiguous_opening():
    assert align(SCRIPT, timed_words(SCRIPT,20) + timed_words(SCRIPT,200)) is None


def test_alignment_rejects_short_or_unrelated_transcript():
    assert align('Hello good morning', timed_words(SCRIPT)) is None
    assert align(SCRIPT, timed_words('the weather is extremely cold today ' * 20)) is None


def test_matching_brackets_allow_late_paragraphs():
    from models import Question, QuestionGroup
    q = Question(group=QuestionGroup(group_type='matching', passage='[A] First. [P] Later. [Q] Next. [R] Last.'))
    assert q.allowed_answers == list('ABCDEFGHIJKLMNOPQR')


def test_reading_range_cannot_be_imported_as_listening():
    import parser
    text = 'Questions 46 to 50 are based on the passage you have just heard. 46. A) first B) second C) third D) fourth'
    groups = parser.parse_cet_text(text)
    assert not any(g['type'] == 'listening' for g in groups)


def test_ocr_even_zero_reserve_keeps_four_physical_cores(monkeypatch):
    import ocr_batch
    topology = [(1,[i,i+1]) for i in range(0,16,2)] + [(0,[i]) for i in range(16,20)]
    monkeypatch.setattr(ocr_batch,'cpu_topology',lambda:topology)
    used=set(ocr_batch.choose_cores(0))
    assert sum(not used.intersection(ids) for _,ids in topology)==4
    assert used==set(range(8)) | set(range(16,20))
