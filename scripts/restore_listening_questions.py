"""Add missing listening questions from a reviewed, source-hashed plan.

Existing rows, including learner records, must remain byte-for-byte equivalent.
Defaults to read-only validation; an applied plan is idempotent.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata


ROOT = Path(__file__).resolve().parents[1]
TABLES = ('question_group', 'question', 'option', 'user_history', 'wrong_question')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def option_signature(options):
    return tuple(re.sub(r'\W+', '', unicodedata.normalize('NFKC', options.get(label, '')).casefold()) for label in 'ABCD')


def row_fingerprints(conn, limits):
    result = {}
    for table, limit in limits.items():
        h, count = hashlib.sha256(), 0
        for row in conn.execute(f'SELECT * FROM "{table}" WHERE id<=? ORDER BY id', (limit,)):
            h.update(json.dumps(tuple(row), ensure_ascii=True, separators=(',', ':')).encode())
            h.update(b'\n')
            count += 1
        result[table] = {'rows': count, 'sha256': h.hexdigest()}
    return result


def validate_evidence(candidate, root):
    kinds = set()
    for evidence in candidate.get('evidence', []):
        path = (root / evidence['path']).resolve(strict=True)
        if not path.is_relative_to(root) or sha256(path) != evidence['sha256']:
            raise ValueError(f'Evidence changed or outside workspace: {path}')
        if path.suffix.lower() == '.pdf' and evidence.get('visual_confirmed') is True:
            pages = evidence.get('pages_1based', [])
            if not pages or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in pages):
                raise ValueError('Visual PDF evidence needs physical page numbers')
            import fitz
            with fitz.open(path) as pdf:
                if max(pages) > len(pdf):
                    raise ValueError('Evidence page outside PDF')
            if path.name != candidate['source_file']:
                raise ValueError('Source-paper identity differs from candidate')
            kinds.add('answers' if path.parent.name == 'answers' else 'questions')
    if kinds != {'questions', 'answers'}:
        raise ValueError('Question and answer PDFs both need visual verification')


def validated_candidate(candidate, root):
    if candidate.get('status') not in (None, 'ready'):
        raise ValueError('Only reviewed ready candidates can be restored')
    source, number = candidate['source_file'], candidate['q_number']
    if not re.fullmatch(r'\d{4}-\d{2}-CET[46]-\d(?:-\d)?\.pdf', source):
        raise ValueError('Invalid paper filename')
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 25:
        raise ValueError('Invalid listening question number')
    for field in ('content', 'explanation'):
        value = candidate[field]
        if not isinstance(value, str) or len(value.strip()) < 12 or '\ufffd' in value:
            raise ValueError(f'Incomplete {field}: {source}/{number}')
    choices = candidate['options']
    if set(choices) != set('ABCD') or any(not isinstance(v, str) or not v.strip() or '\ufffd' in v for v in choices.values()):
        raise ValueError('Exactly four complete A-D options required')
    if candidate['correct_answer'] not in choices:
        raise ValueError('Answer is not an available option')
    title = candidate['title']
    match = re.fullmatch(r'Listening: Questions (\d+) to (\d+)', title)
    if not match or not int(match[1]) <= number <= int(match[2]) <= 25:
        raise ValueError('Question outside reviewed group range')
    validate_evidence(candidate, root)


def restore(database, plan, apply=False, root=ROOT):
    root = Path(root).resolve(strict=True)
    database = Path(database).resolve(strict=True)
    before_hash = sha256(database)
    candidates = plan['candidates']
    seen = set()
    for candidate in candidates:
        validated_candidate(candidate, root)
        identity = (candidate['source_file'], candidate['q_number'])
        if identity in seen:
            raise ValueError(f'Duplicate planned question: {identity}')
        seen.add(identity)
    conn = sqlite3.connect(database.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True)
    conn.row_factory = sqlite3.Row
    backup, added, new_groups, unchanged = None, [], [], []
    pending = []
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        limits = {table: conn.execute(f'SELECT coalesce(max(id),0) FROM "{table}"').fetchone()[0] for table in TABLES}
        protected_before = row_fingerprints(conn, limits)
        group_definitions = {}
        group_ids = {}
        for candidate in candidates:
            source, title = candidate['source_file'], candidate['title']
            key = (source, title)
            rows = conn.execute("SELECT * FROM question_group WHERE source_file=? AND title=? AND group_type='listening'", key).fetchall()
            if len(rows) > 1:
                raise ValueError(f'Ambiguous target group: {key}')
            group = rows[0] if rows else None
            if candidate.get('group_id') is not None and (group is None or group['id'] != candidate['group_id']):
                raise ValueError(f'Stale group identity: {key}')
            if group is None:
                definition = candidate.get('group')
                if not definition or definition.get('title') != title:
                    raise ValueError(f'New group definition missing: {key}')
                if not isinstance(definition.get('transcript'), str) or len(definition['transcript'].strip()) < 100:
                    raise ValueError('New listening group needs a complete transcript')
                start = definition.get('audio_start')
                if start is not None:
                    import math
                    if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0:
                        raise ValueError('Invalid new-group audio start')
                if key in group_definitions and definition != group_definitions[key]:
                    raise ValueError('Inconsistent definitions for the same new group')
                group_definitions[key] = definition
            else:
                group_ids[key] = group['id']
                if not conn.execute('SELECT 1 FROM question WHERE group_id=? LIMIT 1', (group['id'],)).fetchone():
                    raise ValueError(f'Empty pre-existing group requires a separately reviewed complete restoration: {key}')
                definition = candidate.get('group')
                if definition and any(group[field] != value for field, value in {
                    'title': title, 'passage': definition.get('passage', 'Audio context required.'),
                    'transcript': definition['transcript'], 'audio_start': definition.get('audio_start')
                }.items()):
                    raise ValueError(f'Restored group no longer matches reviewed definition: {key}')
                expected = candidate.get('existing_question_numbers')
                actual = [r[0] for r in conn.execute('SELECT q_number FROM question WHERE group_id=? ORDER BY q_number,id', (group['id'],))]
                # A rerun may include this plan's already restored positions.
                planned_numbers = {x['q_number'] for x in candidates if (x['source_file'], x['title']) == key}
                if expected is not None and sorted(n for n in actual if n not in planned_numbers) != sorted(expected):
                    raise ValueError(f'Existing question span changed: {key}')
            existing = conn.execute('SELECT * FROM question WHERE source_file=? AND q_number=?', (source, candidate['q_number'])).fetchall()
            if existing:
                if len(existing) != 1:
                    raise ValueError('Duplicate existing question position')
                row = existing[0]
                fields = {'group_id': group['id'] if group else None, 'source_file': source,
                          'section': candidate.get('section', 'Listening'), 'q_number': candidate['q_number'],
                          'content': candidate['content'], 'correct_answer': candidate['correct_answer'], 'explanation': candidate['explanation']}
                choices = dict(conn.execute('SELECT label,text FROM option WHERE question_id=?', (row['id'],)))
                if any(row[k] != v for k, v in fields.items()) or choices != candidate['options']:
                    raise ValueError(f'Question position already occupied by different data: {source}/{candidate["q_number"]}')
                unchanged.append({'source_file': source, 'q_number': candidate['q_number'], 'id': row['id']})
            else:
                for other in conn.execute('SELECT id,q_number FROM question WHERE source_file=?', (source,)).fetchall():
                    choices = dict(conn.execute('SELECT label,text FROM option WHERE question_id=?', (other['id'],)))
                    if set(choices) == set('ABCD') and option_signature(choices) == option_signature(candidate['options']):
                        raise ValueError(f'Options already belong to an existing source question; check misnumbering: {source}/{other["q_number"]}')
                pending.append(candidate)
        # Every newly created group must be restored in full, with no placeholders.
        for key in group_definitions:
            match = re.fullmatch(r'Listening: Questions (\d+) to (\d+)', key[1])
            expected = list(range(int(match[1]), int(match[2]) + 1))
            actual = sorted(c['q_number'] for c in pending if (c['source_file'], c['title']) == key)
            if actual != expected:
                raise ValueError(f'New group is incomplete: {key}')
        if apply and pending:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = database.with_name(database.name + f'.bak-{stamp}-question-recovery')
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as source_conn:
                with sqlite3.connect(backup) as target:
                    source_conn.backup(target)
            for key, definition in group_definitions.items():
                cursor = conn.execute('INSERT INTO question_group(source_file,title,passage,group_type,audio_start,transcript) VALUES(?,?,?,?,?,?)',
                                      (key[0], key[1], definition.get('passage', 'Audio context required.'), 'listening', definition.get('audio_start'), definition['transcript']))
                group_ids[key] = cursor.lastrowid
                new_groups.append({'source_file': key[0], 'title': key[1], 'id': cursor.lastrowid})
            for candidate in pending:
                key = (candidate['source_file'], candidate['title'])
                cursor = conn.execute('INSERT INTO question(group_id,source_file,section,content,q_number,correct_answer,explanation) VALUES(?,?,?,?,?,?,?)',
                                      (group_ids[key], candidate['source_file'], candidate.get('section', 'Listening'), candidate['content'], candidate['q_number'], candidate['correct_answer'], candidate['explanation']))
                question_id = cursor.lastrowid
                conn.executemany('INSERT INTO option(question_id,label,text) VALUES(?,?,?)',
                                 [(question_id, label, candidate['options'][label]) for label in 'ABCD'])
                added.append({'source_file': key[0], 'q_number': candidate['q_number'], 'id': question_id, 'group_id': group_ids[key]})
            # Read back every inserted field; triggers cannot silently alter new data.
            for candidate, identity in zip(pending, added):
                row = conn.execute('SELECT * FROM question WHERE id=?', (identity['id'],)).fetchone()
                expected = {'group_id': identity['group_id'], 'source_file': candidate['source_file'],
                            'section': candidate.get('section', 'Listening'), 'content': candidate['content'],
                            'q_number': candidate['q_number'], 'correct_answer': candidate['correct_answer'],
                            'explanation': candidate['explanation']}
                options = dict(conn.execute('SELECT label,text FROM option WHERE question_id=?', (identity['id'],)))
                if row is None or any(row[k] != v for k, v in expected.items()) or options != candidate['options']:
                    raise ValueError('Inserted question or options do not match the reviewed values')
            for key, definition in group_definitions.items():
                row = conn.execute('SELECT * FROM question_group WHERE id=?', (group_ids[key],)).fetchone()
                expected = {'source_file': key[0], 'title': key[1], 'group_type': 'listening',
                            'passage': definition.get('passage', 'Audio context required.'),
                            'audio_start': definition.get('audio_start'), 'transcript': definition['transcript']}
                if row is None or any(row[k] != v for k, v in expected.items()):
                    raise ValueError('Inserted group does not match the reviewed values')
        protected_after = row_fingerprints(conn, limits)
        if protected_before != protected_after:
            raise ValueError('Existing data changed; rolling back')
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        foreign_keys = [tuple(row) for row in conn.execute('PRAGMA foreign_key_check')]
        if integrity != 'ok' or foreign_keys:
            raise ValueError('Database integrity check failed; rolling back')
        counts = {table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in TABLES}
        expected_additions = {'question_group': len(new_groups), 'question': len(added), 'option': 4 * len(added), 'user_history': 0, 'wrong_question': 0}
        if any(counts[t] != protected_before[t]['rows'] + expected_additions[t] for t in TABLES):
            raise ValueError('Unexpected row additions; rolling back')
        conn.commit() if apply else conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {'applied': apply, 'database': str(database), 'backup': str(backup) if backup else None,
            'sha256_before': before_hash, 'sha256_after': sha256(database), 'pending_questions': len(pending),
            'inserted_questions': added, 'inserted_groups': new_groups, 'already_present': unchanged,
            'existing_rows_unchanged': protected_before == protected_after,
            'protected_existing_rows': protected_after, 'counts': counts,
            'integrity_check': integrity, 'foreign_key_errors': foreign_keys}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=ROOT / 'instance/cet4_v2.db')
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.report and args.report.resolve() in {args.database.resolve(), args.plan.resolve()}:
        parser.error('--report must not overwrite the database or source plan')
    result = restore(args.database, json.loads(args.plan.read_text(encoding='utf-8')), apply=args.apply)
    result['plan_sha256'] = sha256(args.plan)
    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
