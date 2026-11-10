"""Narrow, evidence-checked corrections for already installed question banks.

Keep these rules in Python so frozen launchers collect them with their imports.
They are not a general seed-to-user synchronization: each repair requires an
exact known old value and a matching, verified replacement in the new seed.
"""
from __future__ import annotations

import hashlib
import sqlite3


_TABLES = ("question_group", "question", "option", "user_history", "wrong_question")
_Q3253_IDENTITY = ("2020-12-CET6-1.pdf", 26, "A")
_Q3253_OLD_SHA256 = "e5923d26ef15b8dcaf239af3dba00598b3c4ce4fa509c0efe61711f4d012a748"
# Verified against data/answers/2020-12-CET6-1.pdf, PDF page 7 (printed p.15),
# SHA-256 7b6603b90f820fbb90ce20b8d7f5edd8f16cec0eb8d2ccc767a0d62f5d72895f.
Q3253_EXPLANATION = (
    "【考点】动词辨析题。\n\n"
    "【语法判断】空格前面是破折号，说明空格所在部分是对前面内容的补充说明。"
    "空格后是名词短语，逗号后是 while 引导的现在分词结构，由此可知空格处应填入与 providing 平行的动词现在分词形式。\n\n"
    "【语义判断】本句提到，向不利于社会的东西征税使得双重利益成为可能。"
    "破折号后说明双重利益是什么，其中之一与有害活动有关。"
    "第二段第二句指出，对酒征税可以减少过度饮酒，由此可知，此处应该表示这种税收可以减少或者阻止有害活动。"
    "备选的动词现在分词形式中，只有 A) discouraging“设法劝阻，阻止”符合句意。"
)
_Q3253_OPTIONS = tuple(
    (88791 + index, 3253, label, text)
    for index, (label, text) in enumerate((
        ("A", "discouraging"), ("B", "dividend"), ("C", "emotional"),
        ("D", "fragments"), ("E", "impaired"), ("F", "imprisoned"),
        ("G", "incentives"), ("H", "inherently"), ("I", "initially"),
        ("J", "instrumental"), ("K", "merging"), ("L", "predict"),
        ("M", "probably"), ("N", "pump"), ("O", "swelling"),
    ))
)


def _target(connection):
    required = {"id", "source_file", "q_number", "correct_answer", "explanation"}
    columns = {row[1] for row in connection.execute('PRAGMA table_info("question")')}
    if not required.issubset(columns):
        return None
    row = connection.execute(
        "SELECT source_file, q_number, correct_answer, explanation FROM question WHERE id=3253"
    ).fetchone()
    if row is None or tuple(row[:3]) != _Q3253_IDENTITY:
        return None
    option_columns = {item[1] for item in connection.execute('PRAGMA table_info("option")')}
    if not {"id", "question_id", "label", "text"}.issubset(option_columns):
        return None
    options = tuple(connection.execute(
        "SELECT id, question_id, label, text FROM option WHERE question_id=3253 ORDER BY id"
    ))
    return row if options == _Q3253_OPTIONS else None


def _snapshot(connection):
    # The fixed whitelist mirrors the launcher's educational and personal tables.
    result = {}
    for table in _TABLES:
        columns = tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))
        if "id" not in columns:
            raise ValueError("题库表结构不完整，已取消可信解析修正。")
        result[table] = (columns, tuple(connection.execute(f'SELECT * FROM "{table}" ORDER BY id')))
    return result


def apply_verified_corrections(connection, source) -> int:
    """Repair only the known truncated Q3253 explanation, inside an upgrade.

    The caller owns the transaction and must roll it back on any exception.
    Missing targets, older seeds, and locally edited answers/options/explanations
    are deliberately left untouched. No attempt is made to infer exam answers.
    """
    bundled = _target(source)
    if bundled is None or bundled[3] != Q3253_EXPLANATION:
        return 0
    installed = _target(connection)
    if installed is None or not isinstance(installed[3], str):
        return 0
    if hashlib.sha256(installed[3].encode("utf-8")).hexdigest() != _Q3253_OLD_SHA256:
        return 0
    if not connection.in_transaction:
        raise ValueError("可信解析修正必须在题库升级事务内执行。")

    before = _snapshot(connection)
    columns, rows = before["question"]
    id_index, explanation_index = columns.index("id"), columns.index("explanation")
    expected_rows = []
    for row in rows:
        if row[id_index] == 3253:
            row = row[:explanation_index] + (Q3253_EXPLANATION,) + row[explanation_index + 1:]
        expected_rows.append(row)
    expected = {**before, "question": (columns, tuple(expected_rows))}

    def authorize(action, table, column, database, trigger):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            if (action == sqlite3.SQLITE_UPDATE and database == "main" and table == "question"
                    and column == "explanation" and trigger is None):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        changed = connection.execute(
            "UPDATE question SET explanation=? WHERE id=3253 AND source_file=? "
            "AND q_number=? AND correct_answer=? AND explanation=?",
            (Q3253_EXPLANATION, *_Q3253_IDENTITY, installed[3]),
        ).rowcount
        if changed != 1 or _snapshot(connection) != expected:
            raise ValueError("可信解析修正影响了其他内容，已取消本次更新。")
    finally:
        connection.set_authorizer(None)
    return 1
