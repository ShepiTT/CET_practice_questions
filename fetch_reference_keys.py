"""Cache published reference keys and compare question identity before filling blanks.

The default is a read-only proposal. Existing keys, including disagreements, are
never overwritten. HTML snapshots and SHA256 hashes make every proposal auditable.
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

ROOT = Path(__file__).resolve().parent


def norm(text):
    return re.sub(r'[^a-z0-9]', '', text.lower())


def similarity(a, b):
    return SequenceMatcher(None, norm(a), norm(b), autojunk=False).ratio()


def passage_prefix_matches(reference, local):
    # OCR punctuation/contractions may differ; require a long near-identical run.
    needle, haystack = norm(reference)[:160], norm(local)
    if len(needle) < 60:
        return False
    match = SequenceMatcher(None, needle, haystack, autojunk=False).find_longest_match()
    offset = match.b - match.a
    return any(SequenceMatcher(None, needle, haystack[i:i+len(needle)], autojunk=False).ratio() >= .94
               for i in range(max(0, offset-5), max(0, offset)+6))


def read_reference(source):
    year, month, level, version = Path(source).stem.split('-')
    url = f'https://english-exam.lazynote.cn/{level.lower()}/paper/{year}-{month}-{version}/'
    path = ROOT / '.cache/reference' / (Path(source).stem + '.html')
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
        root = html.fromstring(body)
        if not root.xpath('//*[@class="wp-answers__table"]'):
            raise ValueError('No reference answer table')
        path.write_bytes(body)
    body = path.read_bytes()
    root = html.fromstring(body.decode('utf-8'))
    keys = {}
    for cell in root.xpath('//*[@class="wp-answers__cell"]'):
        for number, answer in re.findall(r'\b(\d{1,2})\s+([A-Z])\b', cell.text_content()):
            number = int(number)
            if number in keys:
                raise ValueError('Duplicate reference number')
            keys[number] = answer
    return root, keys, {'url': url, 'sha256': hashlib.sha256(body).hexdigest()}


def validate_question(conn, q, root, key):
    options = dict(conn.execute('SELECT label,text FROM option WHERE question_id=?', (q['id'],)))
    if q['group_type'] == 'cloze':
        bank = root.xpath('//*[@id="choices-part3-section-a"]//div[span[2]]')
        reference = {e[0].text_content().strip(' )'): e[1].text_content() for e in bank}
        passage = root.xpath('//*[@id="p-part3-section-a-1"]')
        if len(reference) != 15 or not passage:
            return None
        # Identity must match both the bank and the numbered blank's passage.
        if any(label not in options or similarity(options[label], word) < .8 for label, word in reference.items()):
            return None
        return .94 if passage_prefix_matches(passage[0].text_content(), q['passage']) and key in options and norm(options[key]) == norm(reference[key]) else None
    elements = root.xpath(f'//*[@id="qp-{q["q_number"]}"]')
    if len(elements) != 1:
        return None
    node = elements[0]
    stem = node.xpath('.//*[@data-pdh-stem]')
    scores = [similarity(q['content'], stem[0].text_content())] if stem else []
    if q['group_type'] == 'matching':
        paragraphs = root.xpath(f'//*[starts-with(@id,"p-part3-section-b-")]//*[@data-pdh-letter="{key}"]')
        local = re.search(rf'(?:^|\s)[\[（(]?{key}\s*[)）\]](.*?)(?=\s[\[（(]?[A-Z]\s*[)）\]]|$)', q['passage'], re.S)
        if not paragraphs or not local or not scores:
            return None
        scores.append(similarity(local.group(1), paragraphs[0].text_content()))
    else:
        reference = {e.get('data-pdh-letter'): e.text_content() for e in node.xpath('.//*[@data-pdh-letter]')}
        if set(reference) != set(options) or key not in options or len(options) != 4:
            return None
        scores.extend(similarity(options[label], value) for label, value in reference.items())
    return min(scores) if scores and min(scores) >= .94 else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--all', action='store_true', help='Audit existing keys as well as missing keys')
    args = parser.parse_args()
    db = ROOT / 'instance/cet4_v2.db'
    report = {'proposed': [], 'unresolved': [], 'conflicts': [], 'existing_agree': 0, 'sources': {}}
    reference_cache = {}

    def reference(name):
        if name not in reference_cache:
            reference_cache[name] = read_reference(name)
        return reference_cache[name]
    with sqlite3.connect(f'{db.as_uri()}?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        clause = '' if args.all else ' WHERE correct_answer IS NULL OR trim(correct_answer)=""'
        papers = [r[0] for r in conn.execute('SELECT DISTINCT source_file FROM question'+clause+' ORDER BY source_file')]
        for source in papers:
            try:
                root, keys, evidence = reference(source)
                report['sources'][source] = evidence
            except Exception as exc:
                print(f'UNAVAILABLE {source}: {exc}', flush=True)
                continue
            count = 0
            for q in conn.execute('SELECT q.*,g.group_type,g.passage FROM question q JOIN question_group g ON g.id=q.group_id WHERE q.source_file=?', (source,)):
                key = keys.get(q['q_number'])
                score = validate_question(conn, q, root, key) if key else None
                selected = evidence
                if score is None and (args.all or not q['correct_answer']):
                    candidates = []
                    for version in ('1', '2', '3'):
                        other = re.sub(r'-[123]\.pdf$', f'-{version}.pdf', source)
                        if other == source:
                            continue
                        try:
                            other_root, other_keys, other_evidence = reference(other)
                        except Exception:
                            continue
                        other_key = other_keys.get(q['q_number'])
                        other_score = validate_question(conn, q, other_root, other_key) if other_key else None
                        if other_score:
                            candidates.append((other_score, other_key, other_evidence))
                    if candidates and len({candidate[1] for candidate in candidates}) == 1:
                        score, key, selected = max(candidates, key=lambda candidate: candidate[0])
                item = {'id': q['id'], 'source': source, 'number': q['q_number'], 'answer': key, 'identity_score': score, 'reference': selected}
                if score is None:
                    if not q['correct_answer']:
                        report['unresolved'].append(item)
                elif q['correct_answer']:
                    if q['correct_answer'] == key:
                        report['existing_agree'] += 1
                    else:
                        report['conflicts'].append(dict(item, existing=q['correct_answer']))
                else:
                    report['proposed'].append(item)
                    count += 1
            print(f'CHECKED {source}: {count} missing keys verified', flush=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        if args.apply:
            backup = db.with_name(db.name + f'.bak-{stamp}-reference')
            with sqlite3.connect(backup) as dest:
                conn.backup(dest)
            report['backup'] = str(backup)
    if args.apply:
        with sqlite3.connect(db) as conn:
            conn.execute('BEGIN IMMEDIATE')
            for item in report['proposed']:
                cursor = conn.execute('UPDATE question SET correct_answer=? WHERE id=? AND source_file=? AND q_number=? AND (correct_answer IS NULL OR trim(correct_answer)="")', (item['answer'], item['id'], item['source'], item['number']))
                if cursor.rowcount != 1:
                    raise RuntimeError('Question changed during review; rollback')
    report['applied'] = args.apply
    path = ROOT / 'instance' / f'reference-keys-{stamp}.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'AUDIT {path}; proposed={len(report["proposed"])}; conflicts={len(report["conflicts"])}; agree={report["existing_agree"]}', flush=True)


if __name__ == '__main__':
    main()
