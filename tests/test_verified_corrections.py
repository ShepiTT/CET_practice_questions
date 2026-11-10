"""Verified explanation upgrades use disposable databases, never user data."""
import hashlib
import shutil
import sqlite3

import pytest

import desktop_launcher as launcher
import verified_corrections as corrections


OLD_EXPLANATION = (
    "【考点】动词辨析题。\n"
    "A）【语法判断】空格前面是破折号，说明空格所在部\n"
    "分是对前面内容的补充说明。空格后是名词短\n"
    "语，逗号后是while引导的现在分词结构由此可\n"
    "知空格处应填人与 providing平行的动词现在分\n"
    "词形式。\n"
    "语义判断）本句提到向不利于社会的东西征税\n"
    "使得双重利益成为可能。破折号后说明双重利益\n"
    "是什么其中之一与有害活动有关。第二段第二\n"
    "句指出对酒征税可以减少过度饮酒，由此可知\n"
    "此处应该表示这种税收可以减少或者阻止有害\n"
    "活动。备选的动词现在分词形式中，只有"
)
OPTIONS = (
    "discouraging", "dividend", "emotional", "fragments", "impaired",
    "imprisoned", "incentives", "inherently", "initially", "instrumental",
    "merging", "predict", "probably", "pump", "swelling",
)


def _snapshot(path):
    with sqlite3.connect(path) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
                for (table,) in tables}


@pytest.fixture
def installed_correction(tmp_path):
    resources, user = tmp_path / "resources", tmp_path / "user"
    seed = resources / "instance" / "cet4_v2.db"
    seed.parent.mkdir(parents=True)
    with sqlite3.connect(seed) as connection:
        connection.executescript('''
            CREATE TABLE question_group(id INTEGER PRIMARY KEY, source_file TEXT, audio_start REAL);
            CREATE TABLE question(id INTEGER PRIMARY KEY, group_id INTEGER REFERENCES question_group(id),
                source_file TEXT, section TEXT, content TEXT, q_number INTEGER,
                correct_answer TEXT, explanation TEXT);
            CREATE TABLE option(id INTEGER PRIMARY KEY, question_id INTEGER REFERENCES question(id),
                label TEXT, text TEXT, UNIQUE(question_id, label));
            CREATE TABLE user_history(id INTEGER PRIMARY KEY, question_id INTEGER REFERENCES question(id),
                user_answer TEXT, is_correct INTEGER, timestamp TEXT);
            CREATE TABLE wrong_question(id INTEGER PRIMARY KEY, question_id INTEGER REFERENCES question(id), added_at TEXT);
            INSERT INTO question_group VALUES(635, '2020-12-CET6-1.pdf', 0);
        ''')
        connection.execute("INSERT INTO question VALUES(3253, 635, '2020-12-CET6-1.pdf', 'Reading Section A', 'Blank 26', 26, 'A', ?)",
                           (OLD_EXPLANATION,))
        connection.execute("INSERT INTO question VALUES(3254, 635, '2020-12-CET6-1.pdf', 'Reading Section A', 'Blank 27', 27, 'B', 'Keep this explanation')")
        connection.executemany("INSERT INTO option VALUES(?, 3253, ?, ?)",
                               [(88791 + i, chr(ord('A') + i), word) for i, word in enumerate(OPTIONS)])
        connection.execute("INSERT INTO option VALUES(88806, 3254, 'B', 'dividend')")
    database = launcher.prepare_database(resources, user)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO user_history VALUES(91, 3253, 'N', 0, '2026-11-10 11:33:22.123456')")
        connection.execute("INSERT INTO wrong_question VALUES(92, 3253, '2026-11-10 11:33:22.654321')")
        connection.execute("CREATE TABLE my_notes(id INTEGER PRIMARY KEY, note TEXT)")
        connection.execute("INSERT INTO my_notes VALUES(1, 'Personal annotation')")
    with sqlite3.connect(seed) as connection:
        connection.execute("UPDATE question SET explanation=? WHERE id=3253", (corrections.Q3253_EXPLANATION,))
    return resources, user, seed, database


def test_verified_old_explanation_is_repaired_and_every_other_value_is_preserved(installed_correction, monkeypatch):
    resources, user, seed, database = installed_correction
    assert hashlib.sha256(OLD_EXPLANATION.encode('utf-8')).hexdigest() == (
        'e5923d26ef15b8dcaf239af3dba00598b3c4ce4fa509c0efe61711f4d012a748'
    )
    before = _snapshot(database)
    assert launcher.prepare_database(resources, user) == database
    after = _snapshot(database)
    approved = list(before['question'][0])
    approved[-1] = corrections.Q3253_EXPLANATION
    assert after['question'] == [tuple(approved), before['question'][1]]
    for table in ('question_group', 'option', 'user_history', 'wrong_question', 'my_notes'):
        assert after[table] == before[table]
    assert after['question'][0][-1].endswith('只有 A) discouraging“设法劝阻，阻止”符合句意。')
    assert '\nA）' not in after['question'][0][-1]
    backups = list(user.glob('*.bak-seed-*'))
    assert len(backups) == 1 and _snapshot(backups[0]) == before
    with sqlite3.connect(seed) as connection:
        assert connection.execute('SELECT COUNT(*) FROM user_history').fetchone() == (0,)
    # The same-seed fast path stays a true no-op, including its backup behavior.
    unchanged_bytes = database.read_bytes()
    monkeypatch.setattr(launcher, '_seed_digest', lambda _: pytest.fail('Same seed must use the fingerprint cache'))
    monkeypatch.setattr(launcher, 'apply_verified_corrections', lambda *_: pytest.fail('Same seed must not repeat corrections'))
    launcher.prepare_database(resources, user)
    assert database.read_bytes() == unchanged_bytes
    assert list(user.glob('*.bak-seed-*')) == backups


@pytest.mark.parametrize('edit', ['explanation', 'answer', 'option_text', 'option_label', 'option_id', 'extra_option'])
def test_locally_modified_explanation_answer_or_options_are_kept(installed_correction, edit):
    resources, user, _seed, database = installed_correction
    with sqlite3.connect(database) as connection:
        if edit == 'explanation':
            connection.execute("UPDATE question SET explanation='My own completed explanation' WHERE id=3253")
        elif edit == 'answer':
            connection.execute("UPDATE question SET correct_answer='N' WHERE id=3253")
        elif edit == 'option_text':
            connection.execute("UPDATE option SET text='My own option' WHERE id=88791")
        elif edit == 'option_label':
            connection.execute("UPDATE option SET label='Z' WHERE id=88791")
        elif edit == 'option_id':
            connection.execute("UPDATE option SET id=99999 WHERE id=88791")
        else:
            connection.execute("INSERT INTO option VALUES(99999, 3253, 'Z', 'My own extra option')")
    before = _snapshot(database)
    # Reassigned option identities deliberately hit the existing append guard.
    if edit in ('option_id', 'option_label'):
        with pytest.raises(RuntimeError, match='备份'):
            launcher.prepare_database(resources, user)
    else:
        launcher.prepare_database(resources, user)
    after = _snapshot(database)
    for table in launcher._EDUCATIONAL + launcher._PERSONAL + ('my_notes',):
        assert after[table] == before[table]


@pytest.mark.parametrize('seed_edit', ['old_explanation', 'unverified_explanation', 'answer', 'option_text', 'missing_target'])
def test_old_or_unverified_seed_does_not_repair_existing_question(installed_correction, seed_edit):
    resources, user, seed, database = installed_correction
    with sqlite3.connect(seed) as connection:
        if seed_edit in ('old_explanation', 'unverified_explanation'):
            text = OLD_EXPLANATION if seed_edit == 'old_explanation' else corrections.Q3253_EXPLANATION + ' Unverified addition.'
            connection.execute('UPDATE question SET explanation=? WHERE id=3253', (text,))
            connection.execute('UPDATE question_group SET audio_start=1 WHERE id=635')
        elif seed_edit == 'answer':
            connection.execute("UPDATE question SET correct_answer='N' WHERE id=3253")
        elif seed_edit == 'option_text':
            connection.execute("UPDATE option SET text='Changed seed option' WHERE id=88791")
        else:
            connection.execute('DELETE FROM option WHERE question_id=3253')
            connection.execute('DELETE FROM question WHERE id=3253')
    before = _snapshot(database)
    launcher.prepare_database(resources, user)
    after = _snapshot(database)
    for table in launcher._EDUCATIONAL + launcher._PERSONAL + ('my_notes',):
        assert after[table] == before[table]


@pytest.mark.parametrize('edit', ['missing_target', 'source_file', 'q_number'])
def test_correction_itself_is_noop_for_absent_or_different_target(installed_correction, edit):
    _resources, _user, seed, database = installed_correction
    with sqlite3.connect(database) as connection:
        if edit == 'missing_target':
            connection.execute('DELETE FROM option WHERE question_id=3253')
            connection.execute('DELETE FROM question WHERE id=3253')
        elif edit == 'source_file':
            connection.execute("UPDATE question SET source_file='my-paper.pdf' WHERE id=3253")
        else:
            connection.execute('UPDATE question SET q_number=99 WHERE id=3253')
    before = _snapshot(database)
    with sqlite3.connect(database) as connection, sqlite3.connect(seed) as source:
        connection.execute('BEGIN IMMEDIATE')
        assert corrections.apply_verified_corrections(connection, source) == 0
    assert _snapshot(database) == before


@pytest.mark.parametrize('side_effect', [
    "UPDATE question SET explanation='unwanted' WHERE id=3254",
    "UPDATE question SET correct_answer='N' WHERE id=3253",
    "UPDATE option SET text='unwanted' WHERE id=88791",
    "UPDATE user_history SET user_answer='A' WHERE id=91",
    'DELETE FROM wrong_question WHERE id=92',
    "INSERT INTO user_history VALUES(99, 3253, 'A', 1, 'later')",
    "UPDATE my_notes SET note='unwanted' WHERE id=1",
])
def test_trigger_side_effect_rolls_back_repair_and_any_appended_paper(installed_correction, side_effect):
    resources, user, seed, database = installed_correction
    with sqlite3.connect(database) as connection:
        connection.execute(f'''CREATE TRIGGER bad_correction AFTER UPDATE OF explanation ON question
            WHEN NEW.id=3253 BEGIN {side_effect}; END''')
    with sqlite3.connect(seed) as connection:
        connection.execute("INSERT INTO question_group VALUES(800, 'new-paper.pdf', 0)")
        connection.execute("INSERT INTO question VALUES(4000, 800, 'new-paper.pdf', 'Reading', 'New question', 1, 'A', 'New explanation')")
        connection.execute("INSERT INTO option VALUES(90000, 4000, 'A', 'New option')")
    before = _snapshot(database)
    before_bytes = database.read_bytes()
    with pytest.raises(RuntimeError, match='备份') as caught:
        launcher.prepare_database(resources, user)
    assert 'not authorized' not in str(caught.value)
    assert _snapshot(database) == before
    assert database.read_bytes() == before_bytes
    backups = list(user.glob('*.bak-seed-*'))
    assert len(backups) == 1 and _snapshot(backups[0]) == before


def test_correction_row_snapshot_detects_trigger_change_even_without_authorizer(installed_correction):
    _resources, _user, seed, database = installed_correction
    with sqlite3.connect(database) as connection:
        connection.execute('''CREATE TRIGGER changed_other_explanation AFTER UPDATE OF explanation ON question
            WHEN NEW.id=3253 BEGIN UPDATE question SET explanation='unwanted' WHERE id=3254; END''')
    before = _snapshot(database)

    class MissingAuthorizer(sqlite3.Connection):
        def set_authorizer(self, _callback):
            pass  # Exercise the independent row comparison with SQLite triggers intact.

    with sqlite3.connect(seed) as source:
        with pytest.raises(ValueError, match='影响了其他内容'):
            with sqlite3.connect(database, factory=MissingAuthorizer) as connection:
                connection.execute('BEGIN IMMEDIATE')
                corrections.apply_verified_corrections(connection, source)
    assert _snapshot(database) == before


def test_already_corrected_database_survives_moved_seed_without_repair(installed_correction, monkeypatch, tmp_path):
    resources, user, seed, database = installed_correction
    launcher.prepare_database(resources, user)
    original = _snapshot(database)
    relocated = tmp_path / 'moved-installation'
    (relocated / 'instance').mkdir(parents=True)
    shutil.copyfile(seed, relocated / 'instance' / seed.name)
    monkeypatch.setattr(launcher, 'apply_verified_corrections', lambda *_: pytest.fail('Same digest must not repeat corrections'))
    launcher.prepare_database(relocated, user)
    after = _snapshot(database)
    assert len(list(user.glob('*.bak-seed-*'))) == 1
    for table in launcher._EDUCATIONAL + launcher._PERSONAL + ('my_notes',):
        assert after[table] == original[table]
