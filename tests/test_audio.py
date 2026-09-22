import os

import pytest
from sqlalchemy import text

import app as app_module
from app import db, audio_for, ensure_column
from models import QuestionGroup, Question, Option


@pytest.fixture
def audio_dir(tmp_path, monkeypatch):
    d = tmp_path / 'audio'
    d.mkdir()
    (d / 't.mp3').write_bytes(b'ID3' + bytes(4000))
    monkeypatch.setattr(app_module, 'AUDIO_DIR', str(d))
    return d


def make_group(source_file, group_type='listening', audio_start=None):
    group = QuestionGroup(source_file=source_file, title=f'{group_type} of {source_file}',
                          passage='Audio context required.', group_type=group_type, audio_start=audio_start)
    q = Question(group=group, source_file=source_file, section='s', content='Question 1', q_number=1,
                 correct_answer='A')
    q.options = [Option(label=l, text=l) for l in 'ABCD']
    db.session.add(group)
    db.session.commit()
    return group


def test_audio_for_maps_paper_to_existing_mp3(client, audio_dir):
    groups = [make_group('t.pdf'), make_group('other.pdf')]
    assert audio_for(groups) == {'t.pdf': 't.mp3', 'other.pdf': None}


def test_audio_for_without_directory(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, 'AUDIO_DIR', str(tmp_path / 'missing'))
    assert audio_for([make_group('t.pdf')]) == {'t.pdf': None}


def test_audio_route_serves_file_and_ranges(client, audio_dir):
    res = client.get('/audio/t.mp3')
    assert res.status_code == 200
    assert res.data[:3] == b'ID3'
    res = client.get('/audio/t.mp3', headers={'Range': 'bytes=0-9'})
    assert res.status_code == 206
    assert len(res.data) == 10
    assert client.get('/audio/nope.mp3').status_code == 404
    assert client.get('/audio/../app.py').status_code == 404


def test_set_audio_start_persists_and_validates(client):
    g = make_group('t.pdf')
    res = client.post('/set_audio_start', json={'group_id': g.id, 'seconds': 83.4})
    assert res.status_code == 200 and res.get_json()['audio_start'] == 83.4
    assert db.session.get(QuestionGroup, g.id).audio_start == 83.4

    assert client.post('/set_audio_start', json={'group_id': g.id, 'seconds': -1}).status_code == 400
    assert client.post('/set_audio_start', json={'group_id': g.id, 'seconds': 'x'}).status_code == 400
    assert client.post('/set_audio_start', json={'group_id': 999, 'seconds': 1}).status_code == 404

    res = client.post('/set_audio_start', json={'group_id': g.id, 'seconds': None})
    assert res.status_code == 200
    assert db.session.get(QuestionGroup, g.id).audio_start is None


def test_practice_page_shows_player_or_missing_badge(client, audio_dir):
    make_group('t.pdf', audio_start=65)
    make_group('other.pdf')
    html = client.get('/practice/t.pdf').get_data(as_text=True)
    assert 'data-src="/audio/t.mp3"' in html
    assert '(1:05)' in html
    assert '无音频' not in html

    html = client.get('/practice/other.pdf').get_data(as_text=True)
    assert '无音频' in html
    assert 'other.mp3' in html
    assert 'btn-success audio-play' not in html


def test_random_mode_skips_listening_without_audio(client, audio_dir):
    make_group('t.pdf')                             # listening, has audio
    make_group('other.pdf')                         # listening, no audio
    make_group('other.pdf', group_type='reading')   # reading never needs audio
    html = client.get('/random_practice').get_data(as_text=True)
    assert 'listening of t.pdf' in html
    assert 'reading of other.pdf' in html
    assert 'listening of other.pdf' not in html


def test_ensure_column_adds_missing_column(client):
    db.session.execute(text('CREATE TABLE legacy (id INTEGER PRIMARY KEY)'))
    db.session.commit()
    ensure_column('legacy', 'audio_start', 'FLOAT')
    ensure_column('legacy', 'audio_start', 'FLOAT')  # idempotent
    cols = [r[1] for r in db.session.execute(text('PRAGMA table_info(legacy)'))]
    assert cols == ['id', 'audio_start']


def test_transcript_toggle_only_when_present(client, audio_dir):
    g = make_group('t.pdf')
    html = client.get('/practice/t.pdf').get_data(as_text=True)
    assert 'transcript-' not in html
    g.transcript = 'Six people had to move away from their home.'
    db.session.commit()
    html = client.get('/practice/t.pdf').get_data(as_text=True)
    assert f'id="transcript-{g.id}"' in html
    assert 'Six people had to move away' in html
