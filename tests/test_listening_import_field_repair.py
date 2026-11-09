"""Exercise exact listening-field repairs on disposable SQLite databases."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from scripts.repair_listening_import_fields import ROOT, repair, sha256
from test_listening_review import review_database, snapshot


@pytest.fixture
def field_plan(review_database, tmp_path):
    source = '2025-06-CET4-1.pdf'
    evidence = []
    for folder in ('data', 'data/answers'):
        path = tmp_path/folder/source
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/folder/source, path)
        evidence.append({'path': path.relative_to(tmp_path).as_posix(), 'sha256': sha256(path),
                         'pages_1based': [1], 'visual_confirmed': True})
    with sqlite3.connect(review_database) as conn:
        old = conn.execute('SELECT text FROM option WHERE id=1').fetchone()[0]
    return {'changes': [{'table': 'option', 'id': 1, 'question_id': 11, 'field': 'text',
        'old': old, 'new': 'A complete clean original option.', 'source_file': source,
        'q_number': 16, 'evidence': evidence}]}


def question_change(plan, **updates):
    value = deepcopy(plan['changes'][0])
    value.update(table='question', id=11, field='content', old='Question 16',
                 new='What does the speaker recommend doing?')
    value.pop('question_id')
    value.update(updates)
    return value


def test_dry_run_preserves_bytes_and_all_rows(review_database, field_plan, tmp_path):
    before, digest = snapshot(review_database), sha256(review_database)
    files = set(tmp_path.iterdir())
    result = repair(review_database, field_plan, root=tmp_path)
    assert result['pending_fields'] == 1 and result['updated_fields'] == 0
    assert not result['applied'] and result['backup'] is None
    assert snapshot(review_database) == before and sha256(review_database) == digest
    assert set(tmp_path.iterdir()) == files


def test_applies_only_exact_reviewed_fields_with_complete_original_backup(review_database, field_plan, tmp_path):
    before = snapshot(review_database)
    field_plan['changes'] += [question_change(field_plan),
        question_change(field_plan,field='explanation',old='解析十六',new='原答案PDF核准的完整解析。'),
        question_change(field_plan,field='correct_answer',old='B',new='A')]
    result = repair(review_database, field_plan, apply=True, root=tmp_path)
    assert result['updated_fields'] == 4 and result['learner_data_unchanged']
    assert result['unreviewed_fields_unchanged'] and result['integrity_check'] == 'ok'
    assert snapshot(Path(result['backup'])) == before
    after = snapshot(review_database)
    expected = deepcopy(before)
    expected['option'][0] = (*expected['option'][0][:3], field_plan['changes'][0]['new'])
    q = list(expected['question'][0]);q[4],q[7],q[6] = (c['new'] for c in field_plan['changes'][1:])
    expected['question'][0] = tuple(q)
    assert after == expected
    assert 'user_answer' not in json.dumps(result, ensure_ascii=False)
    assert '2026-11-08 10:01:02' not in json.dumps(result)


def test_rerun_is_idempotent_without_a_second_backup(review_database, field_plan, tmp_path):
    repair(review_database, field_plan, apply=True, root=tmp_path)
    before, digest = snapshot(review_database), sha256(review_database)
    files = set(tmp_path.iterdir())
    result = repair(review_database, field_plan, apply=True, root=tmp_path)
    assert result['updated_fields'] == 0 and result['already_present_fields'] == 1
    assert result['backup'] is None and snapshot(review_database) == before
    assert sha256(review_database) == digest and set(tmp_path.iterdir()) == files


@pytest.mark.parametrize('apply',[False, True])
@pytest.mark.parametrize('mutation',[
    {'old':'Stale source value.'}, {'source_file':'2025-06-CET4-2.pdf'},
    {'q_number':17}, {'question_id':12}, {'table':'user_history'},
    {'field':'label'}, {'new':''}, {'new':'Broken \ufffd text'}, {'id':999},
])
def test_invalid_second_change_rejects_complete_batch(review_database, field_plan, tmp_path, apply, mutation):
    before, digest = snapshot(review_database), sha256(review_database)
    bad = deepcopy(field_plan['changes'][0]);bad.update(mutation)
    field_plan['changes'] = [question_change(field_plan),bad]
    with pytest.raises((ValueError, FileNotFoundError)):
        repair(review_database, field_plan, apply=apply, root=tmp_path)
    assert snapshot(review_database) == before and sha256(review_database) == digest
    assert not list(tmp_path.glob('*.bak-*'))


def test_repeated_same_field_is_rejected_even_for_identical_values(review_database, field_plan, tmp_path):
    field_plan['changes'].append(deepcopy(field_plan['changes'][0]))
    with pytest.raises(ValueError,match='Duplicate'):
        repair(review_database, field_plan, apply=True, root=tmp_path)
    assert not list(tmp_path.glob('*.bak-*'))


@pytest.mark.parametrize('mutate',[{'sha256':'0'*64},{'visual_confirmed':False},{'pages_1based':[9999]}])
def test_source_evidence_must_remain_hashed_and_visually_reviewed(review_database, field_plan, tmp_path, mutate):
    before = sha256(review_database)
    field_plan['changes'][0]['evidence'][1].update(mutate)
    with pytest.raises(ValueError):repair(review_database, field_plan, apply=True, root=tmp_path)
    assert sha256(review_database) == before


@pytest.mark.parametrize('new',['E','AA','a'])
def test_answer_must_be_an_available_A_D_label(review_database, field_plan, tmp_path, new):
    field_plan['changes'] = [question_change(field_plan,field='correct_answer',old='B',new=new)]
    with pytest.raises(ValueError,match='A-D'):repair(review_database, field_plan, apply=True, root=tmp_path)


def test_valid_answer_letter_with_missing_option_is_rejected(review_database, field_plan, tmp_path):
    with sqlite3.connect(review_database) as conn:conn.execute("DELETE FROM option WHERE question_id=11 AND label='A'")
    field_plan['changes'] = [question_change(field_plan,field='correct_answer',old='B',new='A')]
    with pytest.raises(ValueError,match='label is missing'):
        repair(review_database, field_plan, apply=True, root=tmp_path)


def test_idempotence_never_bypasses_question_identity(review_database, field_plan, tmp_path):
    repair(review_database, field_plan, apply=True, root=tmp_path)
    field_plan['changes'][0]['question_id'] = 12
    with pytest.raises(ValueError,match='identity'):
        repair(review_database, field_plan, apply=True, root=tmp_path)


def test_second_update_failure_rolls_back_first_update_and_keeps_backup(review_database, field_plan, tmp_path):
    with sqlite3.connect(review_database) as conn:
        conn.execute("""CREATE TRIGGER fail_question BEFORE UPDATE OF content ON question BEGIN
            SELECT RAISE(ABORT,'Simulated write failure'); END""")
    before, digest = snapshot(review_database), sha256(review_database)
    field_plan['changes'].append(question_change(field_plan))
    with pytest.raises(sqlite3.DatabaseError):repair(review_database, field_plan, apply=True, root=tmp_path)
    assert snapshot(review_database) == before and sha256(review_database) == digest
    backups = list(tmp_path.glob('*.bak-*-listening-fields'))
    assert len(backups)==1 and snapshot(backups[0]) == before


@pytest.mark.parametrize('sql',[
    "UPDATE user_history SET user_answer='changed by trigger' WHERE id=7;",
    "INSERT INTO option(id,question_id,label,text) VALUES(99,12,'Z','unexpected');",
    "DELETE FROM wrong_question WHERE id=3;",
])
def test_trigger_cannot_touch_learner_data_insert_or_delete_rows(review_database, field_plan, tmp_path, sql):
    with sqlite3.connect(review_database) as conn:
        conn.execute(f'CREATE TRIGGER unexpected AFTER UPDATE OF text ON option BEGIN {sql} END')
    before = snapshot(review_database)
    with pytest.raises(sqlite3.DatabaseError):repair(review_database, field_plan, apply=True, root=tmp_path)
    assert snapshot(review_database) == before


def test_trigger_on_permitted_column_of_another_row_is_detected_and_rolled_back(review_database, field_plan, tmp_path):
    with sqlite3.connect(review_database) as conn:
        conn.execute("""CREATE TRIGGER unexpected AFTER UPDATE OF text ON option WHEN NEW.id=1 BEGIN
            UPDATE option SET text='unreviewed mutation' WHERE id=2; END""")
    before, digest = snapshot(review_database), sha256(review_database)
    with pytest.raises(ValueError,match='Unreviewed'):
        repair(review_database, field_plan, apply=True, root=tmp_path)
    assert snapshot(review_database)==before and sha256(review_database)==digest


def test_trigger_cannot_replace_reviewed_new_value(review_database, field_plan, tmp_path):
    with sqlite3.connect(review_database) as conn:
        conn.execute("""CREATE TRIGGER unexpected AFTER UPDATE OF text ON option WHEN NEW.id=1 BEGIN
            UPDATE option SET text='different final text' WHERE id=1; END""")
    before = snapshot(review_database)
    with pytest.raises(ValueError,match='Final authorized'):
        repair(review_database, field_plan, apply=True, root=tmp_path)
    assert snapshot(review_database) == before


def test_preexisting_foreign_key_violation_rolls_back_repair(review_database, field_plan, tmp_path):
    with sqlite3.connect(review_database) as conn:
        conn.execute("INSERT INTO wrong_question VALUES(99,999,'private timestamp')")
    before, digest = snapshot(review_database), sha256(review_database)
    with pytest.raises(ValueError,match='foreign-key'):
        repair(review_database, field_plan, apply=True, root=tmp_path)
    assert snapshot(review_database)==before and sha256(review_database)==digest


def question_block(number):
    return f'{number}. A. Following choice A.\nB. Following choice B.\nC. Following choice C.\nD. Following choice D.'


@pytest.fixture
def boundary_plan(review_database, field_plan, tmp_path):
    source='2025-06-CET4-3.pdf'
    clean='A preserved original last option.'
    old=clean+'\n'+question_block(17)
    with sqlite3.connect(review_database) as conn:
        conn.execute("UPDATE option SET text=? WHERE id=4",(old,))
        conn.execute("INSERT INTO question_group VALUES(5,?, 'Listening: Questions 16 to 17','Original group','listening',100,'Original transcript')",(source,))
        conn.execute("INSERT INTO question VALUES(51,5,?,'Listening','Question 16',16,'B','Original third-copy explanation')",(source,))
        choices=conn.execute('SELECT label,text FROM option WHERE question_id=11 ORDER BY label').fetchall()
        conn.executemany('INSERT INTO option VALUES(?,?,?,?)',[(101+i,51,l,t) for i,(l,t) in enumerate(choices)])
    audio=tmp_path/'data/audio';audio.mkdir(exist_ok=True)
    for paper in ('2025-06-CET4-1','2025-06-CET4-3'):
        (audio/(paper+'.mp3')).write_bytes(b'identical fixture recording bytes')
    change=deepcopy(field_plan['changes'][0])
    change.update(id=104,question_id=51,source_file=source,old=old,new=clean,
        boundary_cleanup_only=True,evidence_source_file='2025-06-CET4-1.pdf',donor_question_id=11)
    return {'changes':[change]}


def test_default_different_source_evidence_is_still_rejected(review_database,boundary_plan,tmp_path):
    change=boundary_plan['changes'][0]
    for field in ('boundary_cleanup_only','evidence_source_file','donor_question_id'):
        change.pop(field)
    before=sha256(review_database)
    with pytest.raises(ValueError,match='identity differs'):
        repair(review_database,boundary_plan,apply=True,root=tmp_path)
    assert sha256(review_database)==before


def test_boundary_only_deletion_preserves_retained_text_answers_and_is_idempotent(review_database,boundary_plan,tmp_path):
    before=snapshot(review_database)
    result=repair(review_database,boundary_plan,apply=True,root=tmp_path)
    assert result['updated_fields']==1 and result['unreviewed_fields_unchanged']
    expected=deepcopy(before)
    expected['option'][-1]=(*expected['option'][-1][:3],boundary_plan['changes'][0]['new'])
    assert snapshot(review_database)==expected
    assert result['boundary_cleanup_checks'][0]['other_A_C_options_byte_identical']
    assert result['exact_reviewed_diff'][0]['authenticates_original_third_paper_order'] is False
    assert 'does not authenticate original third-paper option order' in result['boundary_cleanup_checks'][0]['provenance_limit']
    digest=sha256(review_database);files=set(tmp_path.iterdir())
    rerun=repair(review_database,boundary_plan,apply=True,root=tmp_path)
    assert rerun['updated_fields']==0 and rerun['already_present_fields']==1 and rerun['backup'] is None
    assert sha256(review_database)==digest and set(tmp_path.iterdir())==files


def test_donor_cleaned_in_same_batch_is_valid_on_idempotent_rerun(review_database,boundary_plan,tmp_path):
    own=boundary_plan['changes'][0]
    donor={k:deepcopy(v) for k,v in own.items() if k not in ('boundary_cleanup_only','evidence_source_file','donor_question_id')}
    donor.update(id=4,question_id=11,source_file=own['evidence_source_file'])
    boundary_plan['changes'].insert(0,donor)
    assert repair(review_database,boundary_plan,apply=True,root=tmp_path)['updated_fields']==2
    assert repair(review_database,boundary_plan,apply=True,root=tmp_path)['updated_fields']==0


@pytest.mark.parametrize('mutation',[
    {'new':'A rewritten original last option.'},
    {'new':'A preserved original last'},
    {'donor_question_id':12},
    {'donor_question_id':41},
    {'evidence_source_file':'2025-12-CET4-1.pdf'},
    {'table':'question','id':51,'field':'content'},
    {'id':101},
])
def test_boundary_exception_rejects_rewrite_wrong_source_donor_or_non_D_field(review_database,boundary_plan,tmp_path,mutation):
    boundary_plan['changes'][0].update(mutation)
    before=sha256(review_database)
    with pytest.raises(ValueError):repair(review_database,boundary_plan,apply=True,root=tmp_path)
    assert sha256(review_database)==before and not list(tmp_path.glob('*.bak-*'))


def test_boundary_donor_requires_exact_other_choices_and_same_recording(review_database,boundary_plan,tmp_path):
    with sqlite3.connect(review_database) as conn:conn.execute("UPDATE option SET text='different A' WHERE id=101")
    with pytest.raises(ValueError,match='A-C'):
        repair(review_database,boundary_plan,apply=True,root=tmp_path)
    with sqlite3.connect(review_database) as conn:conn.execute('UPDATE option SET text=(SELECT text FROM option WHERE id=1) WHERE id=101')
    (tmp_path/'data/audio/2025-06-CET4-3.mp3').write_bytes(b'a different recording')
    with pytest.raises(ValueError,match='recording bytes'):
        repair(review_database,boundary_plan,apply=True,root=tmp_path)


def test_boundary_exception_cannot_also_change_its_question_answer(review_database,boundary_plan,tmp_path):
    other=deepcopy(boundary_plan['changes'][0])
    other.update(table='question',id=51,field='correct_answer',old='B',new='A')
    boundary_plan['changes'].append(other)
    with pytest.raises(ValueError):repair(review_database,boundary_plan,apply=True,root=tmp_path)


def test_missing_leading_1_marker_needs_explicit_declaration_and_two_following_blocks(review_database,boundary_plan,tmp_path):
    change=boundary_plan['changes'][0]
    old=change['new']+'\n'+question_block(3)+'\n'+question_block(14)+'\n'+question_block(15)
    change.update(q_number=12,old=old)
    with sqlite3.connect(review_database) as conn:
        conn.execute('UPDATE question SET q_number=12 WHERE id IN(11,51)')
        conn.execute("UPDATE question_group SET title='Listening: Questions 12 to 15' WHERE id IN(1,5)")
        conn.execute('UPDATE option SET text=? WHERE id IN(4,104)',(old,))
    with pytest.raises(ValueError,match='declared next-question'):
        repair(review_database,boundary_plan,apply=True,root=tmp_path)
    change['boundary_marker_text']='3'
    assert repair(review_database,boundary_plan,apply=True,root=tmp_path)['updated_fields']==1
    assert repair(review_database,boundary_plan,apply=True,root=tmp_path)['already_present_fields']==1
