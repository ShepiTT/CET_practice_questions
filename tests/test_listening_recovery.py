"""Recovery must be additive, reversible, and reject conflicting evidence."""

from copy import deepcopy
from pathlib import Path
import shutil
import sqlite3

import pytest

from scripts.restore_listening_questions import ROOT, restore, sha256
from test_listening_review import review_database, snapshot


@pytest.fixture
def recovery_plan(tmp_path):
    source = '2025-06-CET4-1.pdf'
    evidence = []
    for folder in ('data', 'data/answers'):
        target = tmp_path / folder / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / folder / source, target)
        evidence.append({'path': str(target.relative_to(tmp_path)), 'sha256': sha256(target),
                         'pages_1based': [1], 'visual_confirmed': True})
    candidate = {'source_file': source, 'q_number': 18, 'group_id': 1,
                 'title': 'Listening: Questions 16 to 18', 'existing_question_numbers': [16, 17],
                 'content': 'What does the speaker recommend doing?',
                 'options': {letter: f'Complete source option {letter}.' for letter in 'ABCD'},
                 'correct_answer': 'B', 'explanation': '原文给出对应事实，四个选项中只有 B 符合这项事实。',
                 'evidence': evidence}
    return {'candidates': [candidate]}


def prepare(database):
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE question_group SET title='Listening: Questions 16 to 18' WHERE id=1")


def test_recovery_dry_run_does_not_change_any_bytes(review_database, recovery_plan, tmp_path):
    prepare(review_database)
    before = sha256(review_database)
    files = set(tmp_path.iterdir())
    result = restore(review_database, recovery_plan, root=tmp_path)
    assert result['pending_questions'] == 1
    assert result['inserted_questions'] == []
    assert result['backup'] is None
    assert sha256(review_database) == before
    assert set(tmp_path.iterdir()) == files


def test_additive_recovery_retains_every_old_row_and_is_idempotent(review_database, recovery_plan, tmp_path):
    prepare(review_database)
    before = snapshot(review_database)
    result = restore(review_database, recovery_plan, apply=True, root=tmp_path)
    assert result['existing_rows_unchanged'] is True
    assert len(result['inserted_questions']) == 1
    assert len(result['inserted_groups']) == 0
    assert snapshot(Path(result['backup'])) == before
    after = snapshot(review_database)
    for table in before:
        old_rows = {r[0]: r for r in before[table]}
        assert {r[0]: r for r in after[table] if r[0] in old_rows} == old_rows
    assert len(after['question']) == len(before['question']) + 1
    assert len(after['option']) == len(before['option']) + 4
    digest = sha256(review_database)
    rerun = restore(review_database, recovery_plan, apply=True, root=tmp_path)
    assert rerun['backup'] is None
    assert rerun['pending_questions'] == 0
    assert sha256(review_database) == digest


def test_conflicting_existing_position_aborts_all_questions(review_database, recovery_plan, tmp_path):
    prepare(review_database)
    conflict = deepcopy(recovery_plan['candidates'][0])
    conflict['q_number'] = 17
    recovery_plan['candidates'].append(conflict)
    before = sha256(review_database)
    with pytest.raises(ValueError, match='already occupied|question span changed'):
        restore(review_database, recovery_plan, apply=True, root=tmp_path)
    assert sha256(review_database) == before
    assert not list(tmp_path.glob('*.bak-*'))


def test_stale_source_hash_or_unviewed_answer_pdf_is_rejected(review_database, recovery_plan, tmp_path):
    prepare(review_database)
    before = sha256(review_database)
    for change in ({'sha256': '0' * 64}, {'visual_confirmed': False}, {'pages_1based': [9999]}):
        altered = deepcopy(recovery_plan)
        altered['candidates'][0]['evidence'][1].update(change)
        with pytest.raises(ValueError):
            restore(review_database, altered, apply=True, root=tmp_path)
        assert sha256(review_database) == before


def test_new_group_requires_all_questions_and_a_complete_transcript(review_database, recovery_plan, tmp_path):
    candidate = recovery_plan['candidates'][0]
    candidate.update(group_id=None, title='Listening: Questions 22 to 23', q_number=22)
    candidate.pop('existing_question_numbers')
    candidate['group'] = {'title': candidate['title'], 'transcript': 'A complete verified recording script. ' * 8,
                          'audio_start': 1210.5}
    before = sha256(review_database)
    with pytest.raises(ValueError, match='New group is incomplete'):
        restore(review_database, recovery_plan, apply=True, root=tmp_path)
    assert sha256(review_database) == before
    second = deepcopy(candidate)
    second['q_number'] = 23
    second['options']['A'] = 'A distinct independently verified answer option.'
    recovery_plan['candidates'].append(second)
    result = restore(review_database, recovery_plan, apply=True, root=tmp_path)
    assert len(result['inserted_questions']) == 2
    assert len(result['inserted_groups']) == 1
    assert result['inserted_questions'][0]['group_id'] == result['inserted_questions'][1]['group_id']
    assert result['existing_rows_unchanged']
    second_result = restore(review_database, recovery_plan, apply=True, root=tmp_path)
    assert second_result['pending_questions'] == 0
    assert second_result['inserted_groups'] == []


def test_invalid_answer_and_stale_group_span_are_rejected(review_database, recovery_plan, tmp_path):
    prepare(review_database)
    before = sha256(review_database)
    wrong_answer = deepcopy(recovery_plan)
    wrong_answer['candidates'][0]['correct_answer'] = 'E'
    with pytest.raises(ValueError, match='Answer is not an available option'):
        restore(review_database, wrong_answer, apply=True, root=tmp_path)
    recovery_plan['candidates'][0]['existing_question_numbers'] = [16]
    with pytest.raises(ValueError, match='question span changed'):
        restore(review_database, recovery_plan, apply=True, root=tmp_path)
    assert sha256(review_database) == before


def test_unexpected_trigger_mutation_rolls_back_and_keeps_original_backup(review_database, recovery_plan, tmp_path):
    prepare(review_database)
    with sqlite3.connect(review_database) as conn:
        conn.execute("""CREATE TRIGGER unexpected_update AFTER INSERT ON question BEGIN
            UPDATE user_history SET user_answer='changed by trigger' WHERE id=7;
        END""")
    before = snapshot(review_database)
    with pytest.raises(ValueError, match='Existing data changed'):
        restore(review_database, recovery_plan, apply=True, root=tmp_path)
    assert snapshot(review_database) == before
    backups = list(tmp_path.glob('*.bak-*-question-recovery'))
    assert len(backups) == 1
    assert snapshot(backups[0]) == before


def test_trigger_cannot_silently_alter_an_inserted_answer(review_database, recovery_plan, tmp_path):
    prepare(review_database)
    with sqlite3.connect(review_database) as conn:
        conn.execute("""CREATE TRIGGER corrupt_new_answer AFTER INSERT ON question BEGIN
            UPDATE question SET correct_answer='A' WHERE id=NEW.id;
        END""")
    before = snapshot(review_database)
    with pytest.raises(ValueError, match='Inserted question or options'):
        restore(review_database, recovery_plan, apply=True, root=tmp_path)
    assert snapshot(review_database) == before


def test_duplicate_options_on_a_misnumbered_existing_question_stop_recovery(review_database, recovery_plan, tmp_path):
    prepare(review_database)
    candidate = recovery_plan['candidates'][0]
    with sqlite3.connect(review_database) as conn:
        conn.executemany('UPDATE option SET text=? WHERE question_id=11 AND label=?',
                         [(text, label) for label, text in candidate['options'].items()])
    before = snapshot(review_database)
    with pytest.raises(ValueError, match='check misnumbering'):
        restore(review_database, recovery_plan, apply=True, root=tmp_path)
    assert snapshot(review_database) == before


def test_pending_source_variants_cannot_be_inserted(review_database, recovery_plan, tmp_path):
    recovery_plan['candidates'][0]['status'] = 'pending'
    before = sha256(review_database)
    with pytest.raises(ValueError, match='reviewed ready candidates'):
        restore(review_database, recovery_plan, apply=True, root=tmp_path)
    assert sha256(review_database) == before
