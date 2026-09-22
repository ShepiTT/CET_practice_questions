import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import db
from models import QuestionGroup, Question, Option, UserHistory, WrongQuestion
from parser import extract_word_bank, parse_cet_pdf_v2, BANK_LABELS
import repair_db

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')


def make_question(answer='B', explanation='because', options=('A', 'B', 'C', 'D')):
    group = QuestionGroup(source_file='t.pdf', title='g', passage='p', group_type='reading')
    q = Question(group=group, source_file='t.pdf', section='s', content='c', q_number=1,
                 correct_answer=answer, explanation=explanation)
    q.options = [Option(label=l, text=f'text {l}') for l in options]
    db.session.add(group)
    db.session.commit()
    return q


# ---------------------------------------------------------------- bug 1: reveal must be read-only

def test_reveal_returns_answer_without_recording_anything(client):
    q = make_question()
    res = client.get(f'/answer/{q.id}')
    assert res.status_code == 200
    assert res.get_json() == {'correct_answer': 'B', 'explanation': 'because'}
    assert UserHistory.query.count() == 0
    assert WrongQuestion.query.count() == 0


def test_reveal_unknown_question_is_404(client):
    assert client.get('/answer/999').status_code == 404


@pytest.mark.parametrize('payload', [
    {'answer': 'SHOW_ANSWER_REQUEST'},  # what the old button used to send
    {'answer': ''},
    {},
])
def test_submit_rejects_non_answers(client, payload):
    q = make_question()
    res = client.post('/submit_answer', json={'question_id': q.id, **payload})
    assert res.status_code == 400
    assert UserHistory.query.count() == 0
    assert WrongQuestion.query.count() == 0


def test_submit_wrong_then_right_records_attempts(client):
    q = make_question(answer='B')
    res = client.post('/submit_answer', json={'question_id': q.id, 'answer': 'a'})
    assert res.get_json()['is_correct'] is False
    assert WrongQuestion.query.count() == 1

    res = client.post('/submit_answer', json={'question_id': q.id, 'answer': 'B'})
    assert res.get_json()['is_correct'] is True
    assert UserHistory.query.count() == 2
    assert WrongQuestion.query.count() == 1  # a later correct answer does not auto-remove it


# ---------------------------------------------------------------- bug 2: duplicate options

def test_duplicate_label_is_rejected_by_schema(client):
    q = make_question()
    db.session.add(Option(question_id=q.id, label='A', text='again'))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_options_are_served_in_label_order(client):
    q = make_question(options=('D', 'A', 'C', 'B'))
    db.session.expire(q)
    assert [o.label for o in q.options] == ['A', 'B', 'C', 'D']


@pytest.mark.parametrize('raw, expected', [
    # clean two-column bank
    ('A) adult I) emotional B) associated J) implies',
     {'A': 'adult', 'B': 'associated', 'I': 'emotional', 'J': 'implies'}),
    # zero printed instead of O
    ('G) dental 0) underneath H) downward',
     {'G': 'dental', 'H': 'downward', 'O': 'underneath'}),
    # stray letter between I and ')'
    ('A)actually ID)literary B)approximately D)component',
     {'A': 'actually', 'B': 'approximately', 'D': 'component', 'I': 'literary'}),
    # ')' dropped
    ('B)expansion J potential C)forth',
     {'B': 'expansion', 'C': 'forth', 'J': 'potential'}),
    # stray letter before the label, zero for O with a stray letter before it (2021-06 set 2)
    ('C) consequences H) idiom OM) splitting D) debating E) dimensions J) pushing M 0) touches',
     {'C': 'consequences', 'D': 'debating', 'E': 'dimensions', 'H': 'idiom', 'J': 'pushing',
      'M': 'splitting', 'O': 'touches'}),
    # lower-case L for I (2021-06 set 3)
    ('H)prompted M)variety l)roughly N) voyage',
     {'H': 'prompted', 'I': 'roughly', 'M': 'variety', 'N': 'voyage'}),
    # passage tail before the bank must not win over the real label
    ('(A) has nothing to do with it. A) acknowledge I) implies',
     {'A': 'acknowledge', 'I': 'implies'}),
])
def test_extract_word_bank_tolerates_pdf_glitches(raw, expected):
    assert {o['label']: o['text'] for o in extract_word_bank(raw)} == expected


def test_extract_word_bank_is_sorted_by_label():
    assert [o['label'] for o in extract_word_bank('B) b A) a I) i')] == ['A', 'B', 'I']


@pytest.mark.parametrize('pdf, label, word', [
    ('2022-06-CET4-1.pdf', 'O', 'underneath'),
    ('2024-12-CET4-1.pdf', 'I', 'literary'),
    ('2024-12-CET4-2.pdf', 'I', 'readily'),
    ('2025-12-CET4-3.pdf', 'J', 'potential'),
])
def test_real_papers_yield_full_word_bank(pdf, label, word):
    path = os.path.join(DATA_DIR, pdf)
    if not os.path.exists(path):
        pytest.skip(f'{pdf} not present')
    cloze = [g for g in parse_cet_pdf_v2(path) if g['type'] == 'cloze']
    assert len(cloze) == 1
    bank = {o['label']: o['text'] for o in cloze[0]['questions'][0]['options']}
    assert ''.join(sorted(bank)) == BANK_LABELS
    assert bank[label] == word


# ---------------------------------------------------------------- repair script

@pytest.fixture
def legacy_option_table(client):
    """Recreate `option` the way the shipped database has it: without the unique constraint."""
    db.session.execute(text('DROP TABLE option'))
    db.session.execute(text(
        'CREATE TABLE option (id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL REFERENCES question(id), '
        'label VARCHAR(5) NOT NULL, text TEXT NOT NULL)'))
    db.session.commit()


def test_repair_cleans_options_and_history(client, legacy_option_table):
    q = make_question(answer='B', options=('A', 'B', 'C', 'D', 'A', 'B', 'C', 'D'))
    other = make_question(answer='C')
    # orphan option
    db.session.execute(text("INSERT INTO option (question_id, label, text) VALUES (9999, 'A', 'x')"))
    # q: only ever "revealed" -> must leave the wrong list; other: really answered wrong -> stays
    db.session.add_all([
        UserHistory(question_id=q.id, user_answer='SHOW_ANSWER_REQUEST', is_correct=False),
        UserHistory(question_id=q.id, user_answer='B', is_correct=True),
        WrongQuestion(question_id=q.id),
        UserHistory(question_id=other.id, user_answer='A', is_correct=False),
        WrongQuestion(question_id=other.id),
    ])
    db.session.commit()

    assert repair_db.delete_orphan_options() == 1
    assert repair_db.dedupe_options() == 4
    assert repair_db.purge_reveal_history() == (1, 1)
    repair_db.ensure_unique_index()
    db.session.commit()

    db.session.expire_all()
    assert [o.label for o in q.options] == ['A', 'B', 'C', 'D']
    assert Option.query.count() == 8
    assert [h.user_answer for h in UserHistory.query.all()] == ['B', 'A']
    assert [w.question_id for w in WrongQuestion.query.all()] == [other.id]

    db.session.add(Option(question_id=q.id, label='A', text='again'))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


@pytest.mark.skipif(not os.path.exists(os.path.join(DATA_DIR, '2024-12-CET4-1.pdf')),
                    reason='needs the real PDF')
def test_repair_rebuilds_cloze_bank_from_pdf(client):
    group = QuestionGroup(source_file='2024-12-CET4-1.pdf', title='Section A: Banked Cloze',
                          passage='p', group_type='cloze')
    q = Question(group=group, source_file='2024-12-CET4-1.pdf', section='Reading Section A',
                 content='Blank 27', q_number=27, correct_answer='I')
    q.options = [Option(label='A', text='actually'), Option(label='D', text='literary')]
    db.session.add(group)
    db.session.commit()

    rebuilt, skipped = repair_db.rebuild_cloze_banks(DATA_DIR)
    db.session.commit()
    assert rebuilt == ['2024-12-CET4-1.pdf'] and skipped == []
    bank = {o.label: o.text for o in q.options}
    assert ''.join(sorted(bank)) == BANK_LABELS
    assert bank['I'] == 'literary' and bank['D'] == 'component'


# ---------------------------------------------------------------- a question with no key

def test_unanswered_question_is_not_graded(client):
    q = make_question(answer=None, explanation='')
    res = client.post('/submit_answer', json={'question_id': q.id, 'answer': 'B'})
    assert res.status_code == 200
    body = res.get_json()
    assert body['unanswered'] is True
    assert body['is_correct'] is None
    assert body['correct_answer'] is None
    # nothing recorded: no attempt, and it must not land in the wrong-question list
    assert UserHistory.query.count() == 0
    assert WrongQuestion.query.count() == 0


def test_answered_question_still_grades_normally(client):
    q = make_question(answer='B')
    assert client.post('/submit_answer', json={'question_id': q.id, 'answer': 'B'}).get_json()['is_correct'] is True
    assert client.post('/submit_answer', json={'question_id': q.id, 'answer': 'C'}).get_json()['is_correct'] is False
    assert UserHistory.query.count() == 2
    assert WrongQuestion.query.count() == 1


def test_answer_with_stray_whitespace_still_matches(client):
    q = make_question(answer=' b ')
    assert client.post('/submit_answer', json={'question_id': q.id, 'answer': 'B'}).get_json()['is_correct'] is True
