"""Regression cases found while reviewing the running practice app."""
import pytest

from app import db
from models import Question, QuestionGroup, Option, UserHistory, WrongQuestion
from parser import clean_noise, extract_word_bank


def question(kind='reading', answer='A', number=46, source='2025-12-CET4-1.pdf'):
    group = QuestionGroup(source_file=source, title='Test group', group_type=kind,
                          passage='A) First paragraph.\nB) Second paragraph.\nC) Third paragraph.')
    q = Question(group=group, source_file=source, q_number=number, content='Test', correct_answer=answer)
    if kind != 'matching':
        q.options = [Option(label=x, text=x) for x in 'ABCD']
    db.session.add(q)
    db.session.commit()
    return q


@pytest.mark.parametrize('answer', ['ZZ', 'E', '1', 5, ['A'], {'answer': 'A'}, None])
def test_invalid_submission_never_records_attempt(client, answer):
    q = question()
    res = client.post('/submit_answer', json={'question_id': q.id, 'answer': answer})
    assert res.status_code == 400
    assert UserHistory.query.count() == WrongQuestion.query.count() == 0
    assert client.post('/submit_answer', json={'question_id': q.id, 'answer': ' a '}).json['is_correct']


def test_matching_accepts_only_available_paragraphs(client):
    q = question(kind='matching', number=36)
    assert client.post('/submit_answer', json={'question_id': q.id, 'answer': 'D'}).status_code == 400
    assert client.post('/submit_answer', json={'question_id': q.id, 'answer': 'A'}).json['is_correct']


def test_matching_tolerates_missing_internal_ocr_marker(client):
    q = question(kind='matching', number=36, answer='B')
    q.group.passage = 'A) First paragraph.\nSecond paragraph with lost marker.\nC) Last paragraph.'
    db.session.commit()
    assert client.post('/submit_answer', json={'question_id': q.id, 'answer': 'B'}).json['is_correct']


def test_invalid_existing_key_does_not_grade(client):
    q = question(answer='ZZ')
    res = client.post('/submit_answer', json={'question_id': q.id, 'answer': 'A'})
    assert res.json['unanswered'] is True
    assert UserHistory.query.count() == WrongQuestion.query.count() == 0


@pytest.mark.parametrize('answer', ['BOGUS', 'ZZ', 'E', None, 5])
def test_invalid_standard_answer_does_not_overwrite(client, answer):
    q = question()
    assert client.post('/set_answer', json={'question_id': q.id, 'answer': answer}).status_code == 400
    assert db.session.get(Question, q.id).correct_answer == 'A'


def test_standard_answer_can_be_cleared(client):
    q = question()
    assert client.post('/set_answer', json={'question_id': q.id, 'answer': ''}).status_code == 200
    assert not db.session.get(Question, q.id).correct_answer


def test_invalid_batch_is_atomic(client):
    q1 = question(number=46)
    q2 = question(number=47)
    res = client.post('/batch_set_answers', json={'filename': q1.source_file, 'answers': [
        {'q_number': 46, 'answer': 'B', 'explanation': 'new explanation'},
        {'q_number': 47, 'answer': 'ZZ'},
    ]})
    assert res.status_code == 400
    assert db.session.get(Question, q1.id).correct_answer == 'A'
    assert db.session.get(Question, q1.id).explanation is None
    assert db.session.get(Question, q2.id).correct_answer == 'A'


def test_valid_batch_still_works(client):
    q = question()
    res = client.post('/batch_set_answers', json={'filename': q.source_file, 'answers': [
        {'q_number': 46, 'answer': ' b ', 'explanation': 'Because B'},
    ]})
    assert res.status_code == 200 and res.json['updated_count'] == 1
    assert db.session.get(Question, q.id).correct_answer == 'B'


@pytest.mark.parametrize('payload', [[], ['A'], {'question_id': [], 'answer': 'A'},
                                       {'question_id': True, 'answer': 'A'}])
def test_malformed_submit_returns_client_error(client, payload):
    assert client.post('/submit_answer', json=payload).status_code == 400


@pytest.mark.parametrize('seconds', ['NaN', 'Infinity', '-Infinity', True])
def test_audio_rejects_non_finite_start(client, seconds):
    q = question(kind='listening')
    res = client.post('/set_audio_start', json={'group_id': q.group_id, 'seconds': seconds})
    assert res.status_code == 400
    assert db.session.get(QuestionGroup, q.group_id).audio_start is None


def test_refresh_preserves_level(client):
    question(source='2025-12-CET6-1.pdf')
    html = client.get('/random_practice?level=6').get_data(as_text=True)
    assert 'href="/random_practice?level=6"' in html


def test_clean_exam_header_preserves_sentence_and_normal_numbers():
    assert clean_noise('They owed a great deal to 21 ·2025年12月四级真题(第三套) · the education.') == 'They owed a great deal to  the education.'
    text = 'In 2025, 21 people read 12 articles.'
    assert clean_noise(text) == text


def test_zero_with_period_is_recovered_as_bank_label():
    text = '\n'.join(f'{x}. word' for x in 'ABCDEFGHIJKLMN') + '\n0. systematically'
    bank = {item['label']: item['text'] for item in extract_word_bank(text)}
    assert bank['O'] == 'systematically' and len(bank) == 15
