"""Verify actual shared-listening pages and source grading without recording attempts."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import urllib.request

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
TABLES = ('question_group', 'question', 'option', 'user_history', 'wrong_question')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def database_state(conn):
    result = {}
    for table in TABLES:
        h, count = hashlib.sha256(), 0
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY id'):
            h.update(json.dumps(tuple(row), ensure_ascii=True, separators=(',', ':')).encode())
            h.update(b'\n')
            count += 1
        result[table] = {'rows': count, 'sha256': h.hexdigest()}
    return result


def normalized(text):
    return ' '.join(text.split())


def validate(base_url, database):
    config_path = ROOT/'data/shared_listening.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    conn = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    before = database_state(conn)
    results, old_missing = [], 0
    for target, reference in config['references'].items():
        source = reference['source_file']
        source_rows = conn.execute("SELECT q.* FROM question q JOIN question_group g ON q.group_id=g.id WHERE q.source_file=? AND g.group_type='listening' ORDER BY q.q_number,q.id", (source,)).fetchall()
        assert [r['q_number'] for r in source_rows] == list(range(1, 26))
        target_rows = conn.execute("SELECT q.id,q.q_number FROM question q JOIN question_group g ON q.group_id=g.id WHERE q.source_file=? AND g.group_type='listening'", (target,)).fetchall()
        gap = sorted(set(range(1, 26))-{r['q_number'] for r in target_rows})
        old_missing += len(gap)
        expected_nonlistening = {r[0] for r in conn.execute("SELECT q.id FROM question q JOIN question_group g ON q.group_id=g.id WHERE q.source_file=? AND g.group_type!='listening'", (target,))}
        with urllib.request.urlopen(base_url+'/practice/'+target, timeout=20) as response:
            assert response.status == 200
            soup = BeautifulSoup(response.read().decode('utf-8'), 'html.parser')
        notice = soup.select_one('.shared-listening-notice')
        assert notice is not None and notice['data-shared-listening-source'] == source
        assert all(word in notice.get_text() for word in ('共享听力', '25', '省略', '选项顺序', source))
        shown_ids = {int(el['data-q-id']) for el in soup.select('.question-item')}
        source_ids = {r['id'] for r in source_rows}
        assert shown_ids == source_ids | expected_nonlistening
        assert not shown_ids.intersection({r['id'] for r in target_rows})
        for question in source_rows:
            item = soup.select_one(f'.question-item[data-q-id="{question["id"]}"]')
            options = conn.execute('SELECT label,text FROM option WHERE question_id=? ORDER BY label,id', (question['id'],)).fetchall()
            assert [o['label'] for o in options] == list('ABCD')
            assert [b['data-label'] for b in item.select('button.mcq-btn')] == list('ABCD')
            for option in options:
                actual = item.select_one(f'button[data-label="{option["label"]}"] span').get_text(' ', strip=True)
                assert normalized(actual) == normalized(option['text']), (target, question['q_number'], option['label'])
            with urllib.request.urlopen(base_url+f'/answer/{question["id"]}', timeout=20) as response:
                answer = json.load(response)
            assert answer == {'correct_answer': question['correct_answer'], 'explanation': question['explanation']}
        controls = soup.select('.audio-controls')
        for control in controls:
            group_id = int(control['data-group-id'])
            group = conn.execute('SELECT * FROM question_group WHERE id=?', (group_id,)).fetchone()
            assert group['source_file'] == source
            assert float(control['data-audio-start']) == group['audio_start']
            assert control.select_one('.audio-play')['data-src'] == '/audio/'+source.replace('.pdf', '.mp3')
            assert soup.select_one(f'#transcript-{group_id}') is not None
        request = urllib.request.Request(base_url+'/audio/'+source.replace('.pdf', '.mp3'), headers={'Range': 'bytes=0-31'})
        with urllib.request.urlopen(request, timeout=20) as response:
            assert response.status == 206
            with (ROOT/'data/audio'/source.replace('.pdf', '.mp3')).open('rb') as stream:
                assert response.read() == stream.read(32)
        results.append({'selected_paper': target, 'actual_listening_source': source,
                        'listening_question_count': 25, 'group_count': len(controls),
                        'source_question_ids': sorted(source_ids), 'historical_unfilled_variant_numbers': gap,
                        'nonlistening_ids_unchanged': True, 'notice_visible': True,
                        'options_and_readonly_answers_exact': True, 'audio_range_status': 206})
    # Sampling must not surface the old partial copies, even across repeated runs.
    sampling = []
    aliases = set(config['references'])
    for path in ('/special_practice?type=listening&count=10', '/random_practice'):
        with urllib.request.urlopen(base_url+path, timeout=20) as response:
            assert response.status == 200
            soup = BeautifulSoup(response.read().decode('utf-8'), 'html.parser')
        groups = [int(el['data-group-id']) for el in soup.select('.audio-controls')]
        for group_id in groups:
            source = conn.execute('SELECT source_file FROM question_group WHERE id=?', (group_id,)).fetchone()[0]
            assert source not in aliases
        sampling.append({'path': path, 'old_alias_listening_excluded': True})
    after = database_state(conn)
    assert before == after
    assert len(results) == 9 and old_missing == 25
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not list(conn.execute('PRAGMA foreign_key_check'))
    return {'created_at_utc': datetime.now(timezone.utc).isoformat(), 'database': str(database),
            'database_sha256': digest(database), 'configuration_sha256': digest(config_path),
            'summary': {'omitted_papers_resolved_for_practice': len(results), 'displayed_listening_positions': 225,
                        'previous_missing_display_positions_covered': old_missing,
                        'remaining_display_listening_gaps': 0, 'source_group_count': sum(r['group_count'] for r in results),
                        'original_variant_option_orders_restored': 0, 'database_unchanged_by_readback': True,
                        'learner_records_unchanged': True},
            'results': results, 'sampling': sampling, 'all_table_fingerprints': after,
            'limits': ['Shared source questions retain their source options, answer letters and IDs.',
                       'This resolves complete practice for omitted papers; it does not reconstruct their unprinted original option order.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:5000')
    parser.add_argument('--database', type=Path, default=ROOT/'instance/cet4_v2.db')
    parser.add_argument('--output', type=Path, default=ROOT/'reports/shared-listening-live-validation-20261109.json')
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to(ROOT/'reports'):
        parser.error('--output must be a report path')
    result = validate(args.base_url.rstrip('/'), args.database.resolve())
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result['summary'], ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
