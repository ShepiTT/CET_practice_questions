"""Shared listening must keep its actual source, grading IDs, and provenance."""

from contextlib import contextmanager
import hashlib
import random
import re

from flask import template_rendered
import pytest
from sqlalchemy import text

import app as app_module
from app import app, db
from models import Option, Question, QuestionGroup, UserHistory, WrongQuestion


TARGET = '2021-06-CET6-3.pdf'
SOURCE = '2021-06-CET6-2.pdf'
RECORDING = b'ID3\x04\x00\x00 shared verified examination recording'


@contextmanager
def rendered_templates():
    rendered = []

    def record(sender, template, context, **extra):
        rendered.append((template.name, context))

    template_rendered.connect(record, app)
    try:
        yield rendered
    finally:
        template_rendered.disconnect(record, app)


def add_group(source, numbers, kind='listening'):
    numbers = list(numbers)
    group = QuestionGroup(
        source_file=source, group_type=kind,
        title=f'{kind}: Questions {numbers[0]} to {numbers[-1]}',
        transcript='A complete source transcript for this recorded material.',
        audio_start=42.0 + numbers[0],
    )
    for number in numbers:
        question = Question(
            source_file=source, section=kind, q_number=number,
            content=f'{source}: What is stated in question {number}?',
            correct_answer='BDAC'[number % 4],
            explanation=f'第 {number} 题的原文事实支持该来源卷的对应选项。',
        )
        question.options = [Option(label=label, text=f'{source} Q{number} choice {label}.')
                            for label in 'ABCD']
        group.questions.append(question)
    db.session.add(group)
    db.session.commit()
    return group


@pytest.fixture
def shared_paper(client, tmp_path, monkeypatch):
    audio = tmp_path / 'audio'
    audio.mkdir()
    monkeypatch.setattr(app_module, 'AUDIO_DIR', str(audio))
    for filename in (SOURCE, TARGET):
        (audio / filename.replace('.pdf', '.mp3')).write_bytes(RECORDING)
    canonical = [add_group(SOURCE, range(1, 9)),
                 add_group(SOURCE, range(9, 16)),
                 add_group(SOURCE, range(16, 26))]
    # These are the old partial copies. They must remain in storage but must
    # never be spliced together with the canonical answer letters.
    partial = add_group(TARGET, [1, 3])
    reading = add_group(TARGET, [46, 47], kind='reading')
    mapping = {TARGET: {'source_file': SOURCE,
                        'audio_sha256': hashlib.sha256(RECORDING).hexdigest()}}
    monkeypatch.setitem(app.config, 'SHARED_LISTENING_REFERENCES', mapping)
    return {'audio': audio, 'canonical': canonical, 'partial': partial,
            'reading': reading, 'mapping': mapping}


def question_ids(groups):
    return [question.id for group in groups for question in group.questions]


def snapshot():
    return {table: [tuple(row) for row in db.session.execute(
        text(f'SELECT * FROM {table} ORDER BY id')).all()]
        for table in ('question_group', 'question', 'option', 'user_history', 'wrong_question')}


def test_shared_view_uses_whole_source_groups_and_keeps_target_reading(client, shared_paper):
    source_reading = add_group(SOURCE, [46, 47], kind='reading')
    groups, reference = app_module.effective_practice_groups(TARGET)
    listening = [group for group in groups if group.group_type == 'listening']
    assert {group.id for group in listening} == {
        group.id for group in shared_paper['canonical']}
    assert [question.q_number for group in listening for question in group.questions] == list(range(1, 26))
    assert {group.source_file for group in listening} == {SOURCE}
    assert [group.id for group in groups if group.group_type == 'reading'] == [shared_paper['reading'].id]
    assert source_reading.id not in {group.id for group in groups}
    assert shared_paper['partial'].id not in {group.id for group in groups}
    assert reference is not None


def test_unconfigured_paper_keeps_its_own_groups(client, shared_paper):
    groups, reference = app_module.effective_practice_groups(SOURCE)
    assert reference is None
    assert {group.id for group in groups} == {group.id for group in shared_paper['canonical']}


def test_practice_page_identifies_actual_source_and_renders_all_25_real_ids(client, shared_paper):
    response = client.get(f'/practice/{TARGET}')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '共享听力' in html
    assert SOURCE in html and TARGET in html
    assert '25' in html
    assert '省略' in html
    assert '选项顺序' in html
    shown = [int(value) for value in re.findall(r'data-q-id="(\d+)"', html)]
    assert set(shown) == set(question_ids(shared_paper['canonical']) +
                             question_ids([shared_paper['reading']]))
    assert not (set(shown) & set(question_ids([shared_paper['partial']])))
    assert f'data-src="/audio/{SOURCE.replace(".pdf", ".mp3")}"' in html
    assert f'data-src="/audio/{TARGET.replace(".pdf", ".mp3")}"' not in html


def test_shared_get_and_answer_reveal_preserve_all_old_rows(client, shared_paper):
    old_question = shared_paper['partial'].questions[0]
    db.session.add(UserHistory(question_id=old_question.id, user_answer='C', is_correct=False))
    db.session.add(WrongQuestion(question_id=old_question.id))
    db.session.commit()
    before = snapshot()
    canonical_question = shared_paper['canonical'][0].questions[0]
    for path in (f'/practice/{TARGET}', '/random_practice',
                 '/special_practice?type=listening&count=10',
                 f'/answer/{canonical_question.id}'):
        assert client.get(path).status_code == 200
    assert snapshot() == before
    reveal = client.get(f'/answer/{canonical_question.id}').json
    assert reveal['correct_answer'] == canonical_question.correct_answer
    assert reveal['explanation'] == canonical_question.explanation


def test_shared_submission_grades_and_records_the_visible_source_question(client, shared_paper):
    canonical_question = shared_paper['canonical'][1].questions[0]
    partial_question = shared_paper['partial'].questions[0]
    db.session.add(UserHistory(question_id=partial_question.id, user_answer='C', is_correct=False))
    db.session.commit()
    old_history_id = UserHistory.query.one().id
    response = client.post('/submit_answer', json={
        'question_id': canonical_question.id, 'answer': canonical_question.correct_answer})
    assert response.status_code == 200
    assert response.json['is_correct'] is True
    rows = UserHistory.query.order_by(UserHistory.id).all()
    assert [(row.id, row.question_id) for row in rows] == [
        (old_history_id, partial_question.id), (rows[-1].id, canonical_question.id)]
    assert rows[-1].is_correct is True


def test_special_and_random_keep_canonical_once_and_exclude_partial_alias(client, shared_paper, monkeypatch):
    expected = {group.id for group in shared_paper['canonical']}
    inventory = app_module.special_inventory()
    assert {row.id for row in inventory if row.group_type == 'listening'} == expected
    assert shared_paper['reading'].id in {row.id for row in inventory}
    monkeypatch.setattr(random, 'sample', lambda population, k: list(population)[:k])
    for path in ('/random_practice', '/special_practice?type=listening&count=10'):
        with rendered_templates() as rendered:
            response = client.get(path)
        assert response.status_code == 200
        selected = rendered[-1][1]['groups']
        assert {group.id for group in selected if group.group_type == 'listening'} == expected
        assert shared_paper['partial'].id not in {group.id for group in selected}


@pytest.mark.parametrize('fault', [
    'source_missing', 'source_audio_missing', 'target_audio_missing',
    'different_audio', 'wrong_recorded_hash', 'different_exam', 'different_level',
    'only_24_questions', 'duplicate_question_number', 'missing_option', 'invalid_answer',
    'empty_explanation', 'empty_transcript', 'missing_audio_start',
])
def test_unverified_reference_never_silently_replaces_original_questions(client, shared_paper, fault):
    config = shared_paper['mapping'][TARGET]
    canonical = shared_paper['canonical']
    if fault == 'source_missing':
        for group in canonical:
            db.session.delete(group)
    elif fault in ('source_audio_missing', 'target_audio_missing'):
        filename = SOURCE if fault == 'source_audio_missing' else TARGET
        (shared_paper['audio'] / filename.replace('.pdf', '.mp3')).unlink()
    elif fault == 'different_audio':
        (shared_paper['audio'] / TARGET.replace('.pdf', '.mp3')).write_bytes(b'different recording')
    elif fault == 'wrong_recorded_hash':
        config['audio_sha256'] = '0' * 64
    elif fault in ('different_exam', 'different_level'):
        other = '2020-12-CET6-2.pdf' if fault == 'different_exam' else '2021-06-CET4-2.pdf'
        for group in canonical:
            group.source_file = other
            for question in group.questions:
                question.source_file = other
        (shared_paper['audio'] / other.replace('.pdf', '.mp3')).write_bytes(RECORDING)
        config['source_file'] = other
    elif fault == 'only_24_questions':
        db.session.delete(canonical[-1].questions[-1])
    elif fault == 'duplicate_question_number':
        canonical[-1].questions[-1].q_number = 24
    elif fault == 'missing_option':
        canonical[-1].questions[-1].options.pop()
    elif fault == 'invalid_answer':
        canonical[-1].questions[-1].correct_answer = 'E'
    elif fault == 'empty_explanation':
        canonical[-1].questions[-1].explanation = ' \n '
    elif fault == 'empty_transcript':
        canonical[-1].transcript = ' \n '
    elif fault == 'missing_audio_start':
        canonical[-1].audio_start = None
    db.session.commit()
    before = snapshot()
    with rendered_templates() as rendered:
        response = client.get(f'/practice/{TARGET}')
    assert response.status_code == 200
    context = rendered[-1][1]
    # A failed reference must not quietly fall back to the old partial copies.
    assert not any(group.group_type == 'listening' for group in context['groups'])
    assert shared_paper['reading'].id in {group.id for group in context['groups']}
    html = response.get_data(as_text=True)
    assert '共享听力' in html
    assert any(term in html for term in ('未启用', '暂不可用', '未通过', '无法启用', '校验失败'))
    assert snapshot() == before


def test_verification_does_not_reuse_stale_success_after_audio_changes(client, shared_paper):
    first, _ = app_module.effective_practice_groups(TARGET)
    assert {group.id for group in first if group.group_type == 'listening'} == {
        group.id for group in shared_paper['canonical']}
    (shared_paper['audio'] / TARGET.replace('.pdf', '.mp3')).write_bytes(b'changed recording')
    second, _ = app_module.effective_practice_groups(TARGET)
    assert not any(group.group_type == 'listening' and group.source_file == SOURCE for group in second)


@pytest.mark.parametrize('changed_content', ['answer', 'option'])
def test_pinned_source_rejects_later_answer_or_option_changes(client, shared_paper, changed_content):
    from shared_listening import rows_hash

    questions = sorted((question for group in shared_paper['canonical'] for question in group.questions),
                       key=lambda question: (question.q_number, question.id))
    question_rows = [{column.name: getattr(question, column.name)
                      for column in Question.__table__.columns} for question in questions]
    option_rows = [{column.name: getattr(option, column.name)
                    for column in Option.__table__.columns}
                   for question in questions
                   for option in sorted(question.options, key=lambda option: (option.label, option.id))]
    config = shared_paper['mapping'][TARGET]
    config.update(question_rows_sha256=rows_hash(question_rows),
                  option_rows_sha256=rows_hash(option_rows))
    assert app_module.shared_listening_reference(TARGET)['enabled'] is True

    # Both changes still pass ordinary completeness checks. Only the reviewed
    # source fingerprint can detect that the grading evidence has changed.
    if changed_content == 'answer':
        question = questions[10]
        question.correct_answer = 'A' if question.correct_answer != 'A' else 'B'
    else:
        questions[10].options[0].text = 'An altered option that was not reviewed.'
    db.session.commit()
    before = snapshot()
    with rendered_templates() as rendered:
        response = client.get(f'/practice/{TARGET}')
    assert response.status_code == 200
    assert not any(group.group_type == 'listening' for group in rendered[-1][1]['groups'])
    assert app_module.shared_listening_reference(TARGET)['enabled'] is False
    html = response.get_data(as_text=True)
    assert '共享听力' in html and '已变化' in html
    assert snapshot() == before


def test_import_fill_does_not_clone_fictitious_listening_into_configured_omitted_paper(client, shared_paper):
    for group in QuestionGroup.query.filter_by(source_file=TARGET, group_type='listening').all():
        db.session.delete(group)
    db.session.commit()
    old_source_ids = question_ids(shared_paper['canonical'])
    app_module.fill_missing_groups()
    db.session.commit()
    assert QuestionGroup.query.filter_by(source_file=TARGET, group_type='listening').count() == 0
    assert Question.query.filter_by(source_file=TARGET, section='listening').count() == 0
    groups, reference = app_module.effective_practice_groups(TARGET)
    assert reference['enabled'] is True
    assert question_ids([group for group in groups if group.group_type == 'listening']) == old_source_ids
