from app import db, import_paper
from models import QuestionGroup, Question, Option


def parsed(bank_words=('adult', 'chew'), q1_options=('a', 'b', 'c', 'd')):
    """The shape parse_cet_pdf_v2() returns, small enough to reason about."""
    return [
        {'title': 'Listening: Questions 1 to 2', 'passage': 'Audio context required.', 'type': 'listening',
         'questions': [
             {'q_number': 1, 'content': 'Question 1', 'section': 'Listening Comprehension',
              'options': [{'label': l, 'text': t} for l, t in zip('ABCD', q1_options)]},
             {'q_number': 2, 'content': 'Question 2', 'section': 'Listening Comprehension',
              'options': [{'label': l, 'text': l} for l in 'ABCD']},
         ]},
        {'title': 'Section A: Banked Cloze', 'passage': 'p', 'type': 'cloze',
         'questions': [
             {'q_number': 26, 'content': 'Blank 26', 'section': 'Reading Section A',
              'options': [{'label': l, 'text': w} for l, w in zip('AB', bank_words)]},
         ]},
    ]


def test_import_paper_creates_groups_questions_and_options(client):
    import_paper('x.pdf', parsed())
    db.session.commit()
    assert QuestionGroup.query.filter_by(source_file='x.pdf').count() == 2
    assert Question.query.filter_by(source_file='x.pdf').count() == 3
    q1 = Question.query.filter_by(source_file='x.pdf', q_number=1).one()
    assert [(o.label, o.text) for o in q1.options] == [('A', 'a'), ('B', 'b'), ('C', 'c'), ('D', 'd')]
    assert q1.group.title == 'Listening: Questions 1 to 2'


def test_import_paper_again_updates_in_place_without_duplicates(client):
    import_paper('x.pdf', parsed())
    db.session.commit()
    q1 = Question.query.filter_by(source_file='x.pdf', q_number=1).one()
    q1.correct_answer, q1.explanation = 'B', 'why'
    db.session.commit()
    ids_before = {q.id for q in Question.query.all()}

    import_paper('x.pdf', parsed(bank_words=('adult', 'swallow'), q1_options=('w', 'x', 'y', 'z')))
    db.session.commit()

    assert {q.id for q in Question.query.all()} == ids_before          # same rows, updated
    assert QuestionGroup.query.filter_by(source_file='x.pdf').count() == 2
    assert Option.query.count() == 4 + 4 + 2                            # no duplicated options
    q1 = db.session.get(Question, q1.id)
    assert [o.text for o in q1.options] == ['w', 'x', 'y', 'z']
    assert (q1.correct_answer, q1.explanation) == ('B', 'why')         # answers survive a re-parse
    q26 = Question.query.filter_by(source_file='x.pdf', q_number=26).one()
    assert [o.text for o in q26.options] == ['adult', 'swallow']
