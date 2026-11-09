"""Preview or apply individually evidenced listening-text corrections.

Defaults to a dry run. Only transcript/audio_start in nonempty listening groups
can change; answers, questions, options, and learner records are fingerprinted.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_FIELDS = {'transcript', 'audio_start'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unchanged_state(conn):
    """Hash protected data instead of copying personal practice records to reports."""
    state = {}
    for table in ('question', 'option', 'user_history', 'wrong_question', 'question_group'):
        columns = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]
        if not columns:
            raise ValueError(f'Missing table: {table}')
        if table == 'question_group':
            columns = [name for name in columns if name not in ALLOWED_FIELDS]
        h = hashlib.sha256()
        count = 0
        sql = f'SELECT {",".join(columns)} FROM {table} ORDER BY id'
        for row in conn.execute(sql):
            h.update(json.dumps(tuple(row), ensure_ascii=True, separators=(',', ':')).encode())
            h.update(b'\n')
            count += 1
        state[table] = {'rows': count, 'sha256': h.hexdigest()}
    return state


def apply_plan(database, plan, apply=False):
    database = Path(database).resolve(strict=True)
    before_hash = digest(database)
    conn = sqlite3.connect(database.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True)
    conn.row_factory = sqlite3.Row
    pending, seen = [], set()
    backup = None
    try:
        conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        protected_before = unchanged_state(conn)
        for change in plan['changes']:
            field, group_id = change['field'], change['group_id']
            if field not in ALLOWED_FIELDS:
                raise ValueError(f'Unsupported listening field: {field}')
            if (group_id, field) in seen:
                raise ValueError(f'Duplicate change: {group_id}/{field}')
            seen.add((group_id, field))
            row = conn.execute('SELECT * FROM question_group WHERE id=?', (group_id,)).fetchone()
            if (row is None or row['source_file'] != change['source_file']
                    or row['title'] != change['title'] or row['group_type'] != 'listening'):
                raise ValueError(f'Listening identity changed: {group_id}')
            numbers = [r[0] for r in conn.execute(
                'SELECT q_number FROM question WHERE group_id=? ORDER BY q_number,id', (group_id,))]
            if not numbers or ('question_numbers' in change and numbers != change['question_numbers']):
                raise ValueError(f'Listening question span changed: {group_id}')
            value = change['new']
            if field == 'transcript':
                if not isinstance(value, str) or not value.strip() or '\ufffd' in value:
                    raise ValueError(f'Invalid transcript: {group_id}')
            else:
                import math
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError(f'Invalid audio start: {group_id}')
            if row[field] == value:
                continue
            if row[field] != change['old']:
                raise ValueError(f'Stale listening review: {group_id}/{field}')
            pending.append(change)

        if apply and pending:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = database.with_name(database.name + f'.bak-{stamp}-listening-review')
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as source:
                with sqlite3.connect(backup) as target:
                    source.backup(target)
            for change in pending:
                conn.execute(f'UPDATE question_group SET {change["field"]}=? WHERE id=?',
                             (change['new'], change['group_id']))

        protected_after = unchanged_state(conn)
        if protected_after != protected_before:
            raise ValueError('Protected data changed; rolling back')
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        foreign_keys = [tuple(row) for row in conn.execute('PRAGMA foreign_key_check')]
        if integrity != 'ok' or foreign_keys:
            raise ValueError('Database integrity check failed; rolling back')
        counts = dict(conn.execute("""SELECT count(*) AS listening_groups,
            sum(trim(coalesce(transcript,''))='') AS missing_transcripts,
            sum(audio_start IS NULL) AS missing_audio_starts FROM question_group
            WHERE group_type='listening'
            AND EXISTS(SELECT 1 FROM question WHERE group_id=question_group.id)""").fetchone())
        if apply:
            conn.commit()
        else:
            conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {'applied': apply, 'database': str(database), 'backup': str(backup) if backup else None,
            'updated_fields': len(pending), 'updated_transcripts': sum(x['field'] == 'transcript' for x in pending),
            'updated_audio_starts': sum(x['field'] == 'audio_start' for x in pending),
            'sha256_before': before_hash, 'sha256_after': digest(database),
            'integrity_check': integrity, 'foreign_key_errors': foreign_keys,
            'history_unchanged': protected_before['user_history'] == protected_after['user_history']
                                 and protected_before['wrong_question'] == protected_after['wrong_question'],
            'protected_data': protected_after, 'counts': counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=ROOT / 'instance/cet4_v2.db')
    parser.add_argument('--plan', type=Path, default=ROOT / 'reports/listening-review-plan-20261108.json')
    parser.add_argument('--report', type=Path, default=ROOT / 'reports/listening-review-applied-20261108.json')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    for name, expected in plan['evidence_report_sha256'].items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or digest(path) != expected:
            raise ValueError(f'Evidence report changed: {name}')
    result = apply_plan(args.database, plan, args.apply)
    result['plan_sha256'] = digest(args.plan)
    if args.apply:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
