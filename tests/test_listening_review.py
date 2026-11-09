"""Exercise reviewed listening edits against real, disposable SQLite files."""

from copy import deepcopy
import hashlib
from pathlib import Path
import sqlite3

import pytest

from scripts.apply_listening_review import apply_plan


TABLES = ("question_group", "question", "option", "user_history", "wrong_question")


def snapshot(database):
    with sqlite3.connect(database) as conn:
        return {
            table: conn.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
            for table in TABLES
        }


def fingerprint(database):
    return hashlib.sha256(Path(database).read_bytes()).hexdigest()


@pytest.fixture
def review_database(tmp_path):
    database = tmp_path / "listening.sqlite"
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE question_group (
                id INTEGER PRIMARY KEY,
                source_file TEXT NOT NULL,
                title TEXT,
                passage TEXT,
                group_type TEXT,
                audio_start REAL,
                transcript TEXT
            );
            CREATE TABLE question (
                id INTEGER PRIMARY KEY,
                group_id INTEGER REFERENCES question_group(id),
                source_file TEXT NOT NULL,
                section TEXT,
                content TEXT NOT NULL,
                q_number INTEGER,
                correct_answer TEXT,
                explanation TEXT
            );
            CREATE TABLE option (
                id INTEGER PRIMARY KEY,
                question_id INTEGER NOT NULL REFERENCES question(id),
                label TEXT NOT NULL,
                text TEXT NOT NULL,
                UNIQUE(question_id, label)
            );
            CREATE TABLE user_history (
                id INTEGER PRIMARY KEY,
                question_id INTEGER NOT NULL REFERENCES question(id),
                user_answer TEXT,
                is_correct INTEGER,
                timestamp TEXT
            );
            CREATE TABLE wrong_question (
                id INTEGER PRIMARY KEY,
                question_id INTEGER NOT NULL UNIQUE REFERENCES question(id),
                added_at TEXT
            );
        """)
        conn.executemany(
            "INSERT INTO question_group VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "2025-06-CET4-1.pdf", "Listening: Questions 16 to 17",
                 "Audio context required.", "listening", 865.2, "Original transcript."),
                (2, "2025-06-CET4-1.pdf", "Reading: Questions 46 to 46",
                 "Original reading passage.", "reading", None, "Reading text."),
                (3, "2025-06-CET4-1.pdf", "Listening: Questions 22 to 25",
                 "Orphan group must not be changed.", "listening", None, None),
                (4, "2025-12-CET4-1.pdf", "Listening: Questions 1 to 1",
                 "Audio context required.", "listening", None, None),
            ],
        )
        conn.executemany(
            "INSERT INTO question VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (11, 1, "2025-06-CET4-1.pdf", "Listening", "Question 16", 16, "B", "解析十六"),
                (12, 1, "2025-06-CET4-1.pdf", "Listening", "Question 17", 17, "D", "解析十七"),
                (21, 2, "2025-06-CET4-1.pdf", "Reading", "Question 46", 46, "A", "Reading explanation"),
                (41, 4, "2025-12-CET4-1.pdf", "Listening", "Question 1", 1, "C", "Existing answer"),
            ],
        )
        options = [
            (index, question_id, label, f"{question_id}: {label} — 原有选项")
            for index, (question_id, label) in enumerate(
                ((question_id, label) for question_id in (11, 12, 21, 41) for label in "ABCD"), 1
            )
        ]
        conn.executemany("INSERT INTO option VALUES (?, ?, ?, ?)", options)
        conn.executemany(
            "INSERT INTO user_history VALUES (?, ?, ?, ?, ?)",
            [(7, 11, "A", 0, "2026-11-08 10:01:02.123456"),
             (8, 12, "D", 1, "2026-11-08 10:02:03.654321"),
             (9, 11, "B", 1, "2026-11-08 11:02:03.654321")],
        )
        conn.executemany(
            "INSERT INTO wrong_question VALUES (?, ?, ?)",
            [(3, 11, "2026-11-08 10:01:02.123456"), (4, 41, "2026-11-08 09:00:00")],
        )
    return database


def change(**overrides):
    item = {
        "group_id": 1,
        "source_file": "2025-06-CET4-1.pdf",
        "title": "Listening: Questions 16 to 17",
        "question_numbers": [16, 17],
        "field": "transcript",
        "old": "Original transcript.",
        "new": "Corrected transcript: we're checking the complete text.\n保留引号与换行。",
    }
    item.update(overrides)
    return item


def test_default_is_a_read_only_dry_run(review_database):
    before, digest = snapshot(review_database), fingerprint(review_database)
    result = apply_plan(review_database, {"changes": [change()]})

    assert result["applied"] is False
    assert result["backup"] is None
    assert result["history_unchanged"] is True
    assert snapshot(review_database) == before
    assert fingerprint(review_database) == digest
    assert list(review_database.parent.iterdir()) == [review_database]


def test_apply_changes_only_reviewed_fields_and_backs_up_every_original_row(review_database):
    before = snapshot(review_database)
    plan = {"changes": [change(), change(field="audio_start", old=865.2, new=864.55)]}
    result = apply_plan(review_database, plan, apply=True)

    assert result["applied"] is True
    assert result["updated_fields"] == 2
    assert result["history_unchanged"] is True
    backup = Path(result["backup"])
    assert backup.is_file() and backup.resolve() != review_database.resolve()
    assert snapshot(backup) == before

    after = snapshot(review_database)
    for table in TABLES[1:]:
        assert after[table] == before[table], table
    assert after["question_group"][1:] == before["question_group"][1:]
    assert after["question_group"][0][:5] == before["question_group"][0][:5]
    assert after["question_group"][0][5:] == (864.55, plan["changes"][0]["new"])
    assert plan["changes"][0] == change()  # Caller evidence remains reusable.


def test_second_apply_is_idempotent_and_creates_no_second_backup(review_database):
    plan = {"changes": [change(), change(field="audio_start", old=865.2, new=864.55)]}
    apply_plan(review_database, plan, apply=True)
    before, digest = snapshot(review_database), fingerprint(review_database)
    files = set(review_database.parent.iterdir())

    result = apply_plan(review_database, plan, apply=True)

    assert result["updated_fields"] == 0
    assert result["applied"] is True
    assert result["history_unchanged"] is True
    assert result["backup"] is None
    assert snapshot(review_database) == before
    assert fingerprint(review_database) == digest
    assert set(review_database.parent.iterdir()) == files


def test_empty_transcript_and_audio_start_can_be_filled(review_database):
    identity = {
        "group_id": 4, "source_file": "2025-12-CET4-1.pdf",
        "title": "Listening: Questions 1 to 1", "question_numbers": [1], "old": None,
    }
    result = apply_plan(review_database, {"changes": [
        change(**identity, new="Recovered recording transcript."),
        change(**identity, field="audio_start", new=43.11),
    ]}, apply=True)

    assert result["updated_fields"] == 2
    assert snapshot(review_database)["question_group"][3][5:] == (
        43.11, "Recovered recording transcript.")


INVALID_CHANGES = [
    pytest.param(change(old="Stale original text."), id="stale-original-value"),
    pytest.param(change(source_file="other-test-version.pdf"), id="wrong-source-file"),
    pytest.param(change(title="Listening: Questions 19 to 21"), id="wrong-title"),
    pytest.param(change(question_numbers=[16, 99]), id="wrong-question-identity"),
    pytest.param(change(group_id=999), id="missing-group"),
    pytest.param(change(group_id=2, title="Reading: Questions 46 to 46",
                        question_numbers=[46], old="Reading text."), id="non-listening-group"),
    pytest.param(change(group_id=3, title="Listening: Questions 22 to 25",
                        question_numbers=[], old=None), id="group-without-questions"),
    pytest.param(change(field="passage", old="Audio context required."), id="unsupported-field"),
]


@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
@pytest.mark.parametrize("invalid", INVALID_CHANGES)
def test_rejects_invalid_plan_without_any_partial_change(review_database, invalid, apply):
    before, digest = snapshot(review_database), fingerprint(review_database)
    first = change(field="audio_start", old=865.2, new=864.55)
    with pytest.raises(ValueError):
        apply_plan(review_database, {"changes": [first, deepcopy(invalid)]}, apply=apply)

    assert snapshot(review_database) == before
    assert fingerprint(review_database) == digest


@pytest.mark.parametrize("duplicate_new", ["Same duplicate value.", "Conflicting duplicate value."])
def test_rejects_duplicate_group_field_even_if_repeated_value_is_identical(review_database, duplicate_new):
    before, digest = snapshot(review_database), fingerprint(review_database)
    first = change(new="Same duplicate value.")
    with pytest.raises(ValueError):
        apply_plan(review_database, {"changes": [first, change(new=duplicate_new)]}, apply=True)

    assert snapshot(review_database) == before
    assert fingerprint(review_database) == digest


def test_identity_is_checked_even_when_new_value_is_already_present(review_database):
    before = snapshot(review_database)
    with pytest.raises(ValueError):
        apply_plan(review_database, {"changes": [
            change(source_file="wrong.pdf", old="Earlier text.", new="Original transcript.")
        ]}, apply=True)
    assert snapshot(review_database) == before


def test_runtime_write_failure_rolls_back_earlier_valid_update(review_database):
    with sqlite3.connect(review_database) as conn:
        conn.executescript("""
            CREATE TRIGGER fail_second_review_update
            BEFORE UPDATE OF transcript ON question_group
            WHEN NEW.id = 4
            BEGIN
                SELECT RAISE(ABORT, 'Simulated second write failure');
            END;
        """)
    before, digest = snapshot(review_database), fingerprint(review_database)
    plan = {"changes": [change(), change(
        group_id=4, source_file="2025-12-CET4-1.pdf",
        title="Listening: Questions 1 to 1", question_numbers=[1], old=None,
        new="Recovered transcript.",
    )]}

    with pytest.raises((sqlite3.DatabaseError, ValueError)):
        apply_plan(review_database, plan, apply=True)

    assert snapshot(review_database) == before
    assert fingerprint(review_database) == digest
