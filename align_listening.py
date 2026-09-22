"""Verify listening scripts against local audio word timestamps, then fill empty starts.

Default: dry run. Published scripts are matched to local question options before
alignment; web audio timestamps are never copied to a different local recording.
"""
import argparse
from datetime import datetime
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import urllib.request

from lxml import html
from fetch_reference_keys import read_reference, validate_question
from resource_limits import limit_resources

ROOT = Path(__file__).resolve().parent


def tokens(text):
    text = re.sub(r'(?m)^\s*(?:[MW]|Man|Woman)\s*[:：]', '', text)
    text = re.sub(r'[（(\[]\d+(?:-\d+)?[)）\]]', '', text)
    text = re.sub(r'\b(?:[A-Za-z]\.){2,}', lambda m: m[0].replace('.', ''), text)
    return re.findall(r"[a-z]+(?:'[a-z]+)?", text.lower().replace('’', "'"))


def align(script, words):
    needle = tokens(script)[:26]
    if len(needle) < 18:
        return None
    flattened = [(word, item['start'], item['end']) for item in words for word in tokens(item['word'])]
    haystack = [item[0] for item in flattened]
    candidates = []
    # Anchors must include the opening of the script, not a repeated later sentence.
    for offset, word in enumerate(haystack):
        if word not in needle[:2]:
            continue
        for length in range(len(needle)-3, len(needle)+4):
            window = haystack[offset:offset+length]
            matcher = SequenceMatcher(None, needle, window, autojunk=False)
            score = matcher.ratio()
            if score < .86:
                continue
            blocks = matcher.get_matching_blocks()
            first = blocks[0]
            if first.a > 1 or first.b > 1 or (first.size < 2 and max(block.size for block in blocks) < 8):
                continue
            candidates.append((score, offset + first.b))
    if not candidates:
        return None
    score, index = max(candidates)
    start = flattened[index][1]
    alternatives = [s for s, i in candidates if abs(flattened[i][1] - start) > 15]
    if alternatives and score - max(alternatives) < .10:
        return None
    return {'start': max(0, round(start - .65, 2)), 'score': score,
            'script_prefix': ' '.join(needle), 'heard_prefix': ' '.join(haystack[index:index+26])}


def reference_scripts(source):
    year, month, level, version = Path(source).stem.split('-')
    url = f'https://english-exam.lazynote.cn/{level.lower()}/sections/listening/{year}-{month}-{version}/'
    path = ROOT / '.cache/reference-transcripts' / (Path(source).stem + '.html')
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=25) as response:
            content = response.read()
        path.write_bytes(content)
    content = path.read_bytes()
    root = html.fromstring(content.decode('utf-8'))
    result = {}
    for piece in root.xpath('//*[starts-with(@id,"lt-")]'):
        paragraphs = piece.xpath('.//p[@class="lt-en"]')
        if not paragraphs:
            continue
        lines = []
        for p in paragraphs:
            for number in p.xpath('.//*[@aria-hidden="true"]'):
                number.drop_tree()
            lines.append(p.text_content().strip())
        result[int(piece.get('id')[3:])] = '\n'.join(lines)
    return result, {'url': url, 'sha256': hashlib.sha256(content).hexdigest(), 'paper': source}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    # One separate P core; together with OCR and ASR leaves >=4 physical cores free.
    limit_resources([0])
    db = ROOT / 'instance/cet4_v2.db'
    report = {'proposed': [], 'unresolved': []}
    caches, script_caches = {}, {}
    with sqlite3.connect(f'{db.as_uri()}?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        groups = conn.execute('SELECT * FROM question_group WHERE group_type="listening" AND audio_start IS NULL ORDER BY source_file,id').fetchall()
        for group in groups:
            source = group['source_file']
            span = re.search(r'Questions (\d+) to (\d+)', group['title'] or '')
            if not span:
                continue
            begin, end = map(int, span.groups())
            if begin > 25:
                report['unresolved'].append({'id': group['id'], 'source': source, 'span': [begin,end], 'reason': 'invalid_listening_range'})
                continue
            script, evidence = None, None
            questions = conn.execute('SELECT q.*,g.group_type,g.passage FROM question q JOIN question_group g ON g.id=q.group_id WHERE q.group_id=?', (group['id'],)).fetchall()
            # Prefer same set; fall back only after content identity checks.
            alternatives = [source] + [re.sub(r'-[123]\.pdf$', f'-{v}.pdf', source) for v in ('1','2','3') if not source.endswith(f'-{v}.pdf')]
            for alternate in alternatives:
                try:
                    if alternate not in caches:
                        caches[alternate] = read_reference(alternate)
                    root, keys, _ = caches[alternate]
                    verified = sum(validate_question(conn, q, root, keys.get(q['q_number'])) is not None for q in questions)
                    if verified < min(2, len(questions)) or verified < len(questions) - 1:
                        continue
                    if alternate not in script_caches:
                        script_caches[alternate] = reference_scripts(alternate)
                    scripts, provenance = script_caches[alternate]
                    if begin in scripts:
                        script, evidence = scripts[begin], provenance
                        seek = root.xpath(f'//*[@id="lt-{begin}"]//*[@data-listen-seek]')
                        evidence = dict(evidence, reference_start=float(seek[0].get('data-listen-seek')) if seek else None)
                        break
                except Exception as exc:
                    print(f'SOURCE unavailable {alternate}: {exc}', flush=True)
            if not script:
                script = group['transcript']
                evidence = {'type': 'existing_transcript'} if script else None
                if not script:
                    import answers
                    cache_path = ROOT / 'data/ocr/answers' / (Path(source).stem + '.txt')
                    if cache_path.exists():
                        spans = [(int(m[1]),int(m[2])) for g in groups if g['source_file']==source
                                 if (m := re.search(r'Questions (\d+) to (\d+)',g['title'] or ''))]
                        script = answers.parse_transcripts(cache_path.read_text(encoding='utf-8'), spans).get((begin,end))
                        evidence = {'type': 'answer_pdf', 'cache': str(cache_path)} if script else None
            item = {'id': group['id'], 'source': source, 'span': [begin, end], 'reference': evidence}
            asr_path = ROOT / '.cache/asr' / (Path(source).stem + '.json')
            audio_path = ROOT / 'data/audio' / (Path(source).stem + '.mp3')
            if not script or not asr_path.exists() or not audio_path.exists():
                report['unresolved'].append(dict(item, reason='no_script' if not script else 'no_asr'))
                continue
            asr = json.loads(asr_path.read_text(encoding='utf-8'))
            if hashlib.sha256(audio_path.read_bytes()).hexdigest() != asr['sha256']:
                report['unresolved'].append(dict(item, reason='audio_changed'))
                continue
            words = [w for segment in asr['segments'] for w in segment['words']]
            match = align(script, words)
            refined_path = ROOT / '.cache/asr-refined' / f'{group["id"]}.json'
            if match is None and refined_path.exists():
                refined = json.loads(refined_path.read_text(encoding='utf-8'))
                if refined['sha256'] == asr['sha256']:
                    match = align(script, refined['words'])
            if match is None:
                report['unresolved'].append(dict(item, reason='no_unique_alignment', transcript=script))
                continue
            report['proposed'].append(dict(item, **match, transcript=script, audio_sha256=asr['sha256']))
            print(f'MATCH {source} {begin}-{end}: {match["start"]:.2f}s score={match["score"]:.3f}', flush=True)
        # Reject an entire paper if its independently matched starts are out of order.
        invalid = set()
        for source in {item['source'] for item in report['proposed']}:
            rows = sorted((i for i in report['proposed'] if i['source']==source), key=lambda i:i['span'])
            if any(b['start'] <= a['start'] + 10 for a,b in zip(rows, rows[1:])):
                invalid.add(source)
        for item in list(report['proposed']):
            if item['source'] in invalid:
                report['proposed'].remove(item)
                report['unresolved'].append(dict(item, reason='non_monotonic_starts'))
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        if args.apply:
            backup = db.with_name(db.name + f'.bak-{stamp}-audio')
            with sqlite3.connect(backup) as dest:
                conn.backup(dest)
            report['backup'] = str(backup)
    if args.apply:
        with sqlite3.connect(db) as conn:
            conn.execute('BEGIN IMMEDIATE')
            for item in report['proposed']:
                cursor = conn.execute('UPDATE question_group SET audio_start=?,transcript=CASE WHEN transcript IS NULL OR trim(transcript)="" THEN ? ELSE transcript END WHERE id=? AND source_file=? AND audio_start IS NULL', (item['start'], item['transcript'], item['id'], item['source']))
                if cursor.rowcount != 1:
                    raise RuntimeError('Group changed; rollback')
    report['applied'] = args.apply
    target = ROOT / 'instance' / f'listening-alignment-{stamp}.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'AUDIT {target}: {len(report["proposed"])} matched, {len(report["unresolved"])} unresolved', flush=True)


if __name__ == '__main__':
    main()
