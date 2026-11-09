"""Repair explicitly reviewed imported listening fields, with exact CAS.

The default is read-only. Updates never insert/delete rows. Every other field in
every SQLite table, including learner data, is protected by row fingerprints.
Report values are hashes, not learner records or source-field text.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

try:
    from .restore_listening_questions import validate_evidence
except ImportError:
    from restore_listening_questions import validate_evidence


ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {'question': {'content', 'explanation', 'correct_answer'}, 'option': {'text'}}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _value(value):
    if isinstance(value, bytes):
        return {'blob_sha256': hashlib.sha256(value).hexdigest(), 'bytes': len(value)}
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _identifier(value):
    return '"' + value.replace('"', '""') + '"'


def protected_state(conn, authorized):
    """Hash each full row, masking only the exact authorized id/column pairs.

    The sorted multiset of row digests preserves duplicates and works for tables
    without a rowid or a single-column id. sqlite_sequence is included so a
    trigger cannot silently advance it through a transient insertion.
    """
    schema = [tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    result = {}
    for table in tables:
        columns = [r[1] for r in conn.execute(f'PRAGMA table_info({_identifier(table)})')]
        indices = {column: i for i, column in enumerate(columns)}
        digests = []
        for row in conn.execute(f'SELECT * FROM {_identifier(table)}'):
            values = [_value(v) for v in tuple(row)]
            row_id = row[indices['id']] if 'id' in indices else None
            for field in authorized.get((table, row_id), ()):
                values[indices[field]] = {'authorized_field': field}
            digests.append(_digest(values))
        result[table] = {'rows': len(digests), 'sha256': _digest(sorted(digests))}
    return {'schema_sha256': _digest(schema), 'tables': result}


def _positive_id(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'Invalid {name}')


def boundary_pattern(change):
    """Validate deletion of complete following question blocks, never a rewrite.

    The sole optional OCR exception is an explicitly declared missing leading
    "1" on a 11-19 question marker. Two subsequent, correctly numbered complete
    question blocks must then corroborate that boundary. Retained characters
    are compared literally, without whitespace or spelling normalization.
    """
    if change['table'] != 'option' or change['field'] != 'text':
        raise ValueError('Boundary cleanup permits only option.text')
    old, new = change['old'], change['new']
    if not isinstance(old,str) or len(new) >= len(old) or not old.startswith(new) or '\n' in new or '\r' in new:
        raise ValueError('Boundary cleanup must delete only an exact one-line old prefix suffix')
    tail = old[len(new):]
    if not tail.startswith(('\n','\r\n')):
        raise ValueError('Boundary cleanup must begin at the following question line')
    expected = change['q_number'] + 1
    if expected > 25:
        raise ValueError('Boundary cleanup has no following listening question')
    marker = change.get('boundary_marker_text',str(expected))
    if not isinstance(marker,str) or not marker.isdecimal():
        raise ValueError('Invalid explicit boundary marker')
    headings = list(re.finditer(r'(?m)^[ \t]*(\d{1,2})\s*[.)]\s*A\s*[.)]\s*\S',tail))
    if not headings or tail[:headings[0].start()].strip() or headings[0][1] != marker:
        raise ValueError('Removed suffix does not begin with the declared next-question A option')
    logical = [expected] + [int(m[1]) for m in headings[1:]]
    if marker != str(expected):
        if not (11 <= expected <= 19 and marker==str(expected-10) and len(headings)>=3):
            raise ValueError('Explicit boundary marker is not the narrow missing-leading-1 OCR case')
    if logical != list(range(expected,expected+len(headings))) or logical[-1] > 25:
        raise ValueError('Removed question blocks do not have consecutive next-question numbers')
    for i,heading in enumerate(headings):
        end = headings[i+1].start() if i+1<len(headings) else len(tail)
        block = tail[heading.start():end]
        labels = re.findall(r'(?m)^[ \t]*(?:\d{1,2}\s*[.)]\s*)?([A-D])\s*[.)]\s*\S',block)
        if labels != list('ABCD'):
            raise ValueError('Removed question block lacks exact A-D option boundaries')
    return {'removed_question_numbers':logical,'observed_first_question_marker':marker,
            'missing_leading_1_ocr_marker_explicitly_acknowledged':marker!=str(expected)}


def validate_boundary_donor(conn, change, root):
    row=conn.execute('SELECT * FROM option WHERE id=?',(change['id'],)).fetchone()
    if row is None or row['question_id'] != change['question_id'] or row['label'] != 'D':
        raise ValueError('Boundary cleanup permits only the existing option D')
    target=conn.execute('SELECT * FROM question WHERE id=?',(change['question_id'],)).fetchone()
    donor=conn.execute('SELECT * FROM question WHERE id=?',(change['donor_question_id'],)).fetchone()
    if (target is None or donor is None or target['id']==donor['id']
            or donor['source_file'] != change['evidence_source_file']
            or donor['q_number'] != change['q_number']):
        raise ValueError('Boundary donor question source or number differs')
    groups=[]
    for question in (target,donor):
        group=conn.execute('SELECT * FROM question_group WHERE id=?',(question['group_id'],)).fetchone()
        if group is None or group['group_type']!='listening' or group['source_file']!=question['source_file']:
            raise ValueError('Boundary donor or target is not a matching listening group')
        groups.append(group)
    if groups[0]['title'] != groups[1]['title']:
        raise ValueError('Boundary donor has a different listening group span')
    options={}
    for question in (target,donor):
        options[question['id']]=dict(conn.execute('SELECT label,text FROM option WHERE question_id=?',(question['id'],)))
    own, copied=options[target['id']], options[donor['id']]
    if set(own)!=set('ABCD') or set(copied)!=set('ABCD') or any(own[l]!=copied[l] for l in 'ABC'):
        raise ValueError('Boundary donor A-C options are not byte-identical')
    if copied['D'] not in (change['old'],change['new']):
        raise ValueError('Boundary donor D is neither the reviewed original nor its exact cleaned prefix')
    paths=[root/'data/audio'/Path(q['source_file']).with_suffix('.mp3') for q in (target,donor)]
    hashes=[sha256(p) for p in paths]
    if hashes[0] != hashes[1]:
        raise ValueError('Boundary donor does not share the exact recording bytes')
    return {'donor_question_id':donor['id'],'evidence_source_file':donor['source_file'],
        'source_audio_sha256':hashes[0], 'other_A_C_options_byte_identical':True,
        'retained_characters_unchanged':True,
        'provenance_limit':'Deletion of already-appended foreign question blocks only; does not authenticate original third-paper option order or answers.',
        **boundary_pattern(change)}


def validate_change(change, root):
    table, field = change.get('table'), change.get('field')
    if table not in ALLOWED or field not in ALLOWED[table]:
        raise ValueError('Unsupported repair table or field')
    _positive_id(change.get('id'), 'row id')
    _positive_id(change.get('q_number'), 'question number')
    if change['q_number'] > 25:
        raise ValueError('Repair is outside listening question range')
    if not re.fullmatch(r'\d{4}-\d{2}-CET[46]-\d(?:-\d)?\.pdf', change.get('source_file', '')):
        raise ValueError('Invalid source-paper filename')
    if table == 'option':
        _positive_id(change.get('question_id'), 'option question id')
    if 'old' not in change or 'new' not in change:
        raise ValueError('Repair requires explicit old and new values')
    if change['old'] is not None and not isinstance(change['old'], str):
        raise ValueError('Old field value must be text or NULL')
    value = change['new']
    if not isinstance(value, str) or not value.strip() or '\ufffd' in value or '\x00' in value:
        raise ValueError('New field value must be complete nonempty text')
    if field == 'correct_answer' and value not in {'A', 'B', 'C', 'D'}:
        raise ValueError('Answer must be one of A-D')
    if change.get('boundary_cleanup_only') is True:
        boundary_pattern(change)
        _positive_id(change.get('donor_question_id'),'boundary donor question id')
        target=re.fullmatch(r'(\d{4}-\d{2}-CET[46])-3\.pdf',change['source_file'])
        donor=change.get('evidence_source_file','')
        if not target or not re.fullmatch(re.escape(target[1])+r'-(?:1|2)\.pdf',donor):
            raise ValueError('Boundary cleanup requires a same-exam canonical first/second paper donor for a third-paper local copy')
        validate_evidence({**change,'source_file':donor},root)
    else:
        if any(k in change for k in ('evidence_source_file','donor_question_id','boundary_marker_text')):
            raise ValueError('Cross-source evidence requires explicit boundary_cleanup_only')
        validate_evidence(change, root)


def repair(database, plan, apply=False, root=ROOT):
    database, root = Path(database).resolve(strict=True), Path(root).resolve(strict=True)
    changes = plan.get('changes')
    if not isinstance(changes, list):
        raise ValueError('Plan changes must be a list')
    seen, authorized = set(), {}
    for change in changes:
        validate_change(change, root)
        key = (change['table'], change['id'], change['field'])
        if key in seen:
            raise ValueError('Duplicate repair row/field')
        seen.add(key)
        authorized.setdefault(key[:2], set()).add(key[2])
    boundary_changes=[c for c in changes if c.get('boundary_cleanup_only') is True]
    for cleanup in boundary_changes:
        for other in changes:
            if other is cleanup:
                continue
            target_id=other['id'] if other['table']=='question' else other['question_id']
            if target_id==cleanup['question_id']:
                raise ValueError('Boundary-only target question has another planned field change')
    before_hash = sha256(database)
    conn = sqlite3.connect(database.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True)
    conn.row_factory = sqlite3.Row
    pending, already, backup, boundary_checks = [], [], None, []
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        before = protected_state(conn, authorized)
        for change in changes:
            table, field, row_id = change['table'], change['field'], change['id']
            row = conn.execute(f'SELECT * FROM "{table}" WHERE id=?', (row_id,)).fetchone()
            if row is None:
                raise ValueError(f'Repair row missing: {table}/{row_id}')
            question_id = row_id if table == 'question' else row['question_id']
            if table == 'option' and question_id != change['question_id']:
                raise ValueError(f'Option question identity changed: {row_id}')
            question = conn.execute('SELECT * FROM question WHERE id=?', (question_id,)).fetchone()
            if question is None or question['source_file'] != change['source_file'] or question['q_number'] != change['q_number']:
                raise ValueError(f'Question source identity changed: {question_id}')
            group = conn.execute('SELECT * FROM question_group WHERE id=?', (question['group_id'],)).fetchone()
            if group is None or group['group_type'] != 'listening' or group['source_file'] != question['source_file']:
                raise ValueError(f'Non-listening or mismatched group: {question_id}')
            if field == 'correct_answer' and conn.execute(
                    'SELECT 1 FROM option WHERE question_id=? AND label=?', (question_id, change['new'])).fetchone() is None:
                raise ValueError(f'Answer label is missing: {question_id}')
            if change.get('boundary_cleanup_only') is True:
                boundary_checks.append(validate_boundary_donor(conn,change,root))
            # Identity and evidence are verified even for an idempotent rerun.
            if row[field] == change['new']:
                already.append(change)
            elif row[field] != change['old']:
                raise ValueError(f'Stale repair value: {table}/{row_id}/{field}')
            else:
                pending.append(change)
        if apply and pending:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = database.with_name(database.name + f'.bak-{stamp}-listening-fields')
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as source:
                with sqlite3.connect(backup) as target:
                    source.backup(target)
            allowed_columns = {(c['table'], c['field']) for c in pending}
            def authorize(action, table, column, _database, _trigger):
                if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE):
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_UPDATE and (table, column) not in allowed_columns:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            for change in pending:
                cursor = conn.execute(f'UPDATE "{change["table"]}" SET "{change["field"]}"=? WHERE id=? AND "{change["field"]}" IS ?',
                    (change['new'], change['id'], change['old']))
                if cursor.rowcount != 1:
                    raise ValueError('Compare-and-swap did not update exactly one row')
            conn.set_authorizer(None)
            # Triggers writing a different id of an otherwise permitted column
            # are detected by the full-row protection below. Also verify exact
            # final values for every authorized field, including prior no-ops.
            for change in changes:
                value = conn.execute(f'SELECT "{change["field"]}" FROM "{change["table"]}" WHERE id=?',
                                     (change['id'],)).fetchone()
                if value is None or value[0] != change['new']:
                    raise ValueError('Final authorized field differs from reviewed new value')
            # A donor can be cleaned earlier in the same plan, but neither that
            # nor an idempotent target can bypass the exact-copy checks.
            for change in boundary_changes:
                validate_boundary_donor(conn,change,root)
        after = protected_state(conn, authorized)
        if before != after:
            raise ValueError('Unreviewed field, row, learner data or schema changed; rolling back')
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        foreign_keys = [tuple(row) for row in conn.execute('PRAGMA foreign_key_check')]
        if integrity != 'ok' or foreign_keys:
            raise ValueError('Database integrity or foreign-key check failed; rolling back')
        conn.commit() if apply else conn.rollback()
    except BaseException:
        conn.set_authorizer(None)
        conn.rollback()
        raise
    finally:
        conn.close()
    def brief(change):
        item={'table': change['table'], 'id': change['id'], 'field': change['field'],
                'source_file': change['source_file'], 'q_number': change['q_number'],
                'old_value_sha256': _digest(change['old']), 'new_value_sha256': _digest(change['new'])}
        if change.get('boundary_cleanup_only') is True:
            item.update(boundary_cleanup_only=True,evidence_source_file=change['evidence_source_file'],
                        donor_question_id=change['donor_question_id'],
                        authenticates_original_third_paper_order=False)
        return item
    return {'applied': apply, 'database': str(database), 'backup': str(backup) if backup else None,
        'sha256_before': before_hash, 'sha256_after': sha256(database),
        'pending_fields': len(pending), 'updated_fields': len(pending) if apply else 0,
        'already_present_fields': len(already), 'exact_reviewed_diff': [brief(c) for c in pending],
        'unreviewed_fields_unchanged': before == after,
        'learner_data_unchanged': all(before['tables'].get(t) == after['tables'].get(t)
                                      for t in ('user_history', 'wrong_question')),
        'protected_state': after, 'integrity_check': integrity, 'foreign_key_error_count': len(foreign_keys),
        'boundary_cleanup_checks':boundary_checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=ROOT/'instance/cet4_v2.db')
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    inputs = {args.database.resolve(), args.plan.resolve()}
    if args.report and args.report.resolve() in inputs:
        parser.error('--report must not overwrite the database or source plan')
    result = repair(args.database, json.loads(args.plan.read_text(encoding='utf-8')), apply=args.apply)
    result['plan_sha256'] = sha256(args.plan)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
