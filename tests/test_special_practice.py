"""Special practice selects intact groups without changing learning records."""
from contextlib import contextmanager
from html import unescape
import re
from urllib.parse import parse_qs, urlsplit

from flask import template_rendered
import pytest

import app as app_module
from app import app, db
from models import Option, Question, QuestionGroup, UserHistory, WrongQuestion


@pytest.fixture
def audio_dir(tmp_path, monkeypatch):
    directory = tmp_path / 'audio'
    directory.mkdir()
    monkeypatch.setattr(app_module, 'AUDIO_DIR', str(directory))
    return directory


def add_group(kind='reading', level='4', set_id=1, size=2, audio_dir=None):
    source = f'2025-12-CET{level}-{set_id}.pdf'
    group = QuestionGroup(source_file=source, title=f'{kind} group {level}-{set_id}',
                          group_type=kind, passage='A) First paragraph.\nB) Second paragraph.')
    for index in range(size):
        question = Question(source_file=source, q_number=46 + index,
                            content=f'Question in {kind} {level}-{set_id}: {index}',
                            correct_answer='A', explanation='Because A.')
        question.options = [Option(label=label, text=f'Choice {label}') for label in 'ABCD']
        group.questions.append(question)
    db.session.add(group)
    db.session.commit()
    if audio_dir is not None:
        (audio_dir / source.replace('.pdf', '.mp3')).write_bytes(b'ID3' + bytes(32))
    return group


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


def special_context(client, query):
    with rendered_templates() as rendered:
        response = client.get('/special_practice', query_string=query)
    assert response.status_code == 200
    assert len(rendered) == 1
    template, context = rendered[0]
    assert template == 'practice.html'
    assert context['mode'] == 'special'
    return response, context


def special_links(response):
    links = re.findall(r'href="([^"]+)"', response.get_data(as_text=True))
    return [urlsplit(unescape(link)) for link in links
            if urlsplit(unescape(link)).path == '/special_practice']


def test_home_exposes_special_practice(client):
    response = client.get('/')
    assert response.status_code == 200
    assert special_links(response)
    assert '专项' in response.get_data(as_text=True)


def test_special_selector_offers_all_types_and_level_controls(client, audio_dir):
    response = client.get('/special_practice')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '专项' in html
    for kind in ('listening', 'cloze', 'matching', 'reading'):
        assert kind in html
    assert 'level' in html and 'count' in html


def test_selector_counts_match_eligible_groups_per_level(client, audio_dir):
    add_group(level='4', size=2)
    add_group(level='6', size=3)
    add_group(kind='cloze', size=0)
    add_group(kind='listening', set_id=2, size=4)
    add_group(kind='listening', size=1, audio_dir=audio_dir)
    with rendered_templates() as rendered:
        assert client.get('/special_practice').status_code == 200
    counts = rendered[0][1]['special_counts']
    assert counts['all']['reading'] == {'groups': 2, 'questions': 5}
    assert counts['4']['reading'] == {'groups': 1, 'questions': 2}
    assert counts['6']['reading'] == {'groups': 1, 'questions': 3}
    assert counts['all']['listening'] == {'groups': 1, 'questions': 1}
    assert counts['all']['cloze'] == {'groups': 0, 'questions': 0}


@pytest.mark.parametrize('kind', ['listening', 'cloze', 'matching', 'reading'])
def test_type_filter_keeps_complete_group_and_question_controls(client, audio_dir, kind):
    selected = add_group(kind=kind, size=3, audio_dir=audio_dir)
    other_type = 'reading' if kind != 'reading' else 'cloze'
    excluded = add_group(kind=other_type, set_id=2, audio_dir=audio_dir)
    response, context = special_context(client, {'type': kind})
    assert [group.id for group in context['groups']] == [selected.id]
    html = response.get_data(as_text=True)
    for question in selected.questions:
        assert f'data-q-id="{question.id}"' in html
    for question in excluded.questions:
        assert f'data-q-id="{question.id}"' not in html
    if kind == 'listening':
        assert 'data-src="/audio/2025-12-CET4-1.mp3"' in html
    elif kind == 'matching':
        assert 'matching-input' in html
    elif kind == 'cloze':
        assert 'cloze-select' in html and 'Vocabulary Bank' in html
    else:
        assert 'mcq-btn' in html


def test_group_questions_display_in_exam_number_order(client, audio_dir):
    group = add_group(size=3)
    first, second, third = group.questions
    first.q_number, second.q_number, third.q_number = 48, 46, 47
    expected_ids = [second.id, third.id, first.id]
    db.session.commit()
    response, _ = special_context(client, {'type': 'reading'})
    shown_ids = [int(value) for value in re.findall(r'data-q-id="(\d+)"', response.get_data(as_text=True))]
    assert shown_ids == expected_ids


@pytest.mark.parametrize('level', ['4', '6'])
def test_level_filter_does_not_mix_cet_pools(client, audio_dir, level):
    groups = {value: add_group(level=value) for value in ('4', '6')}
    _, context = special_context(client, {'type': 'reading', 'level': level, 'count': '10'})
    assert [group.id for group in context['groups']] == [groups[level].id]


@pytest.mark.parametrize('query', [{'type': 'reading', 'count': '10'},
                                  {'type': 'reading', 'level': '', 'count': '10'}])
def test_omitted_or_blank_level_includes_both_levels(client, audio_dir, query):
    expected = {add_group(level=level).id for level in ('4', '6')}
    _, context = special_context(client, query)
    assert {group.id for group in context['groups']} == expected


@pytest.mark.parametrize(('count', 'expected_count'), [(None, 3), ('1', 1), ('10', 7)])
def test_count_limits_groups_and_keeps_every_question(client, audio_dir, count, expected_count):
    available = [add_group(set_id=index, size=2) for index in range(1, 8)]
    query = {'type': 'reading'}
    if count is not None:
        query['count'] = count
    response, context = special_context(client, query)
    chosen = context['groups']
    assert len(chosen) == expected_count
    assert len({group.id for group in chosen}) == expected_count
    assert {group.id for group in chosen} <= {group.id for group in available}
    html = response.get_data(as_text=True)
    assert len(re.findall(r'data-q-id="\d+"', html)) == expected_count * 2


def test_skips_empty_groups_and_listening_without_mp3(client, audio_dir):
    selected = add_group(kind='listening', audio_dir=audio_dir)
    missing_audio = add_group(kind='listening', set_id=2)
    empty = add_group(kind='listening', set_id=3, size=0, audio_dir=audio_dir)
    _, context = special_context(client, {'type': 'listening', 'count': '10'})
    assert {group.id for group in context['groups']} == {selected.id}
    assert missing_audio.id not in {group.id for group in context['groups']}
    assert empty.id not in {group.id for group in context['groups']}


def test_reading_does_not_require_audio_directory(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, 'AUDIO_DIR', str(tmp_path / 'missing'))
    group = add_group()
    _, context = special_context(client, {'type': 'reading'})
    assert [selected.id for selected in context['groups']] == [group.id]


@pytest.mark.parametrize('query', [
    {'type': 'writing'}, {'type': 'READING'},
    {'type': 'reading', 'level': '5'}, {'type': 'reading', 'level': 'CET4'},
    {'type': 'reading', 'count': '0'}, {'type': 'reading', 'count': '-1'},
    {'type': 'reading', 'count': '11'}, {'type': 'reading', 'count': '2.5'},
    {'type': 'reading', 'count': 'NaN'}, {'type': 'reading', 'count': ''},
])
def test_invalid_filters_return_bad_request_without_writes(client, audio_dir, query):
    response = client.get('/special_practice', query_string=query)
    assert response.status_code == 400
    assert UserHistory.query.count() == WrongQuestion.query.count() == 0


def test_refresh_link_preserves_all_filters(client, audio_dir):
    add_group(kind='matching', level='6')
    response, _ = special_context(client, {'type': 'matching', 'level': '6', 'count': '2'})
    query_strings = [parse_qs(link.query) for link in special_links(response)]
    assert {'type': ['matching'], 'level': ['6'], 'count': ['2']} in query_strings
    assert '换一批' in response.get_data(as_text=True)


def test_empty_pool_keeps_filters_and_explains_state(client, audio_dir):
    add_group(level='4')
    response, context = special_context(client, {'type': 'reading', 'level': '6', 'count': '2'})
    assert context['groups'] == []
    html = response.get_data(as_text=True)
    assert any(message in html for message in ('暂无', '没有', '无可用'))
    assert 'Location' not in response.headers
    assert {'type': ['reading'], 'level': ['6'], 'count': ['2']} in [
        parse_qs(link.query) for link in special_links(response)]


def test_empty_cloze_group_is_safe_to_render(client, audio_dir):
    add_group(kind='cloze', size=0)
    response, context = special_context(client, {'type': 'cloze'})
    assert context['groups'] == []
    assert any(message in response.get_data(as_text=True) for message in ('暂无', '没有', '无可用'))


def test_get_and_answer_reveal_do_not_change_history_but_submission_works(client, audio_dir):
    group = add_group()
    question = group.questions[0]
    db.session.add(UserHistory(question_id=question.id, user_answer='B', is_correct=False))
    db.session.add(WrongQuestion(question_id=question.id))
    db.session.commit()
    for query in ({}, {'type': 'reading'}, {'type': 'reading', 'level': '6'}):
        assert client.get('/special_practice', query_string=query).status_code == 200
    assert client.get(f'/answer/{question.id}').json['correct_answer'] == 'A'
    assert UserHistory.query.count() == WrongQuestion.query.count() == 1

    response = client.post('/submit_answer', json={'question_id': question.id, 'answer': 'A'})
    assert response.status_code == 200 and response.json['is_correct'] is True
    assert UserHistory.query.count() == 2
    assert WrongQuestion.query.count() == 1
